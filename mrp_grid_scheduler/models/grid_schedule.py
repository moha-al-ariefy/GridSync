import logging
import math
from datetime import datetime, time, timedelta

import pytz

from markupsafe import Markup
from psycopg2 import OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import format_amount

from ..services.scheduler import (
    BASELINE_METHOD, BOUNDED_METHOD, EXACT_METHOD, MAX_HORIZON, MAX_JOBS, METHOD, STEP, JobInput, ScheduleInputError, WorkcenterInput,
    ceil_slot, dump_inputs, floor_slot, schedule,
)


_logger = logging.getLogger(__name__)

GRID_PLANNER = 'mrp_grid_scheduler.group_grid_planner'
INPUT_FIELDS = {'company_id', 'date_from', 'date_to', 'workcenter_ids', 'objective', 'carbon_price'}
RESULT_FIELDS = {
    'state', 'queued_by', 'compute_error', 'computed_at', 'method', 'currency_id', 'proposed_cost', 'current_cost', 'lower_bound',
    'has_current_cost', 'has_lower_bound', 'complete',
    'proposed_carbon_kg', 'current_carbon_kg', 'shift_penalty', 'carbon_lower_bound_kg', 'job_count', 'moved_count', 'unchanged_count',
    'unscheduled_count', 'data_notes', 'snapshot', 'line_ids',
    'applied_at', 'applied_by', 'reverted_at', 'reverted_by', 'application_snapshot',
}


# Quarter-hour choices for the planning window, matching the 15-minute tariff intervals.
TIME_OPTIONS = [(f'{minutes // 60:02d}:{minutes % 60:02d}', f'{minutes // 60:02d}:{minutes % 60:02d}')
                for minutes in range(0, 24 * 60 + 1, 15)]


def _utc(value):
    """Convert an aware datetime to naive UTC, rounded to the second."""
    value = value.astimezone(pytz.utc).replace(tzinfo=None)
    return (value + timedelta(microseconds=500_000)).replace(microsecond=0)


def _reason_text(env, status, code):
    texts = {
        ('moved', 'optimized'): env._('Moved as part of the lowest-cost complete schedule found.'),
        ('unchanged', 'optimized'): env._('Kept in place in the lowest-cost complete schedule found.'),
        ('moved', 'cheaper'): env._('Moved to a lower-cost time.'),
        ('moved', 'unplanned'): env._('Not planned yet. Placed at the lowest-cost time found.'),
        ('moved', 'current_outside'): env._('Its current time is outside working hours, the period, its deadline or its dependency order.'),
        ('moved', 'current_conflict'): env._('Its current time overlaps another booking in this proposal.'),
        ('moved', 'current_no_prices'): env._('Its current time has missing price intervals.'),
        ('moved', 'duration_changed'): env._('Its current booking does not match the expected working time.'),
        ('unchanged', 'lowest'): env._('Its current time is already the lowest-cost time found.'),
        ('unchanged', 'baseline'): env._('The existing schedule is feasible and no cheaper complete schedule was found.'),
        ('unscheduled', 'no_window'): env._('No working time fits before its deadline or the end of the period.'),
        ('unscheduled', 'conflict'): env._('All suitable working time is already booked.'),
        ('unscheduled', 'retained_booking'): env._('The suggested time would overlap a booking that has to stay in place.'),
        ('unscheduled', 'retained_dependency'): env._('The suggested time would finish after a dependent operation that has to stay in place.'),
        ('unscheduled', 'no_prices'): env._('Price intervals are missing for every suitable time.'),
        ('unscheduled', 'predecessor'): env._('An earlier operation could not be scheduled.'),
        ('unscheduled', 'external_unplanned'): env._('It waits for an operation outside this run that has no planned end.'),
        ('unscheduled', 'cycle'): env._('Its operation dependencies form a loop.'),
    }
    return texts.get((status, code), code)


class GridScheduleRun(models.Model):
    _name = 'grid.schedule.run'
    _description = 'GridSync Schedule Proposal'
    _check_company_auto = True
    _order = 'id desc'

    _queued_runs = models.Index("(id) WHERE state = 'queued'")

    name = fields.Char(required=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('queued', 'Queued'), ('failed', 'Failed'),
        ('proposed', 'Proposed'), ('applied', 'Applied'), ('reverted', 'Reverted'),
    ], default='draft', required=True, readonly=True, copy=False)
    date_from = fields.Datetime('Period start', required=True)
    date_to = fields.Datetime('Period end', required=True)
    # Form helpers: pick the days once, then a start and end time on those days.
    window_start_date = fields.Date('First day', compute='_compute_window_start', readonly=False)
    window_end_date = fields.Date('Last day', compute='_compute_window_end', readonly=False)
    window_start_time = fields.Selection(TIME_OPTIONS, string='Start time', compute='_compute_window_start',
                                         readonly=False)
    window_end_time = fields.Selection(TIME_OPTIONS, string='End time', compute='_compute_window_end',
                                       readonly=False)
    workcenter_ids = fields.Many2many(
        'mrp.workcenter', string='Work centers', check_company=True,
        domain="[('company_id', '=', company_id), ('grid_allow_shift', '=', True)]",
    )
    objective = fields.Selection([
        ('cost', 'Electricity cost'), ('gb_carbon', 'GB electricity emissions'),
        ('gb_combined', 'GB cost and emissions'),
    ], required=True, default='cost')
    carbon_price = fields.Float('Carbon price per tonne (company currency)', digits=(16, 4))
    proposed_carbon_kg = fields.Float('Proposed emissions (kg CO₂)', digits=(20, 6), readonly=True, copy=False)
    current_carbon_kg = fields.Float('Current emissions (kg CO₂)', digits=(20, 6), readonly=True, copy=False)
    carbon_lower_bound_kg = fields.Float('Emissions lower bound (kg CO₂)', digits=(20, 6), readonly=True, copy=False)
    queued_by = fields.Many2one('res.users', readonly=True, copy=False)
    compute_error = fields.Char(readonly=True, copy=False)
    computed_at = fields.Datetime(readonly=True, copy=False)
    method = fields.Char(readonly=True, copy=False)
    method_label = fields.Char(string="Search result", compute="_compute_method_label")
    currency_id = fields.Many2one('res.currency', readonly=True, copy=False)
    shift_penalty = fields.Monetary('Estimated shift penalty', readonly=True, copy=False)
    proposed_cost = fields.Monetary('Proposed cost', readonly=True, copy=False)
    current_cost = fields.Monetary('Cost at current times', readonly=True, copy=False)
    lower_bound = fields.Monetary(readonly=True, copy=False)
    has_current_cost = fields.Boolean(readonly=True, copy=False)
    has_lower_bound = fields.Boolean(readonly=True, copy=False)
    saving = fields.Monetary('Estimated saving', compute='_compute_comparison')
    gap = fields.Monetary('Gap to lower bound', compute='_compute_comparison')
    complete = fields.Boolean(readonly=True, copy=False)
    job_count = fields.Integer('Operations', readonly=True, copy=False)
    moved_count = fields.Integer('Moved', readonly=True, copy=False)
    unchanged_count = fields.Integer('Unchanged', readonly=True, copy=False)
    unscheduled_count = fields.Integer('Not scheduled', readonly=True, copy=False)
    data_notes = fields.Text('Data used', readonly=True, copy=False)
    snapshot = fields.Json(readonly=True, copy=False)
    price_refresh_warning = fields.Boolean(compute='_compute_price_refresh_warning')
    application_snapshot = fields.Json(readonly=True, copy=False)
    applied_at = fields.Datetime(readonly=True, copy=False)
    applied_by = fields.Many2one('res.users', readonly=True, copy=False)
    reverted_at = fields.Datetime(readonly=True, copy=False)
    reverted_by = fields.Many2one('res.users', readonly=True, copy=False)
    line_ids = fields.One2many('grid.schedule.line', 'run_id', string='Proposed operations', readonly=True, copy=False)

    @api.depends('snapshot', 'computed_at', 'state')
    def _compute_price_refresh_warning(self):
        now = fields.Datetime.now()
        for run in self:
            reference = now if run.state == 'proposed' else run.computed_at
            sources = (run.snapshot or {}).get('sources', [])
            cutoff = reference - timedelta(minutes=15) if reference else None
            run.price_refresh_warning = bool(cutoff and any(
                source.get('type') == 'energy_charts' and (
                    not source.get('last_sync_at')
                    or datetime.fromisoformat(source['last_sync_at']) < cutoff
                )
                for source in sources
            ))

    # Cost analysis, derived from the proposal lines.
    energy_kwh = fields.Float('Estimated energy (kWh)', digits=(16, 1), compute='_compute_cost_analysis')
    compared_count = fields.Integer('Operations with both costs', compute='_compute_cost_analysis')
    compared_current_cost = fields.Monetary('Their cost at current times', compute='_compute_cost_analysis')
    compared_proposed_cost = fields.Monetary('Their proposed cost', compute='_compute_cost_analysis')
    compared_saving = fields.Monetary('Their saving', compute='_compute_cost_analysis')
    saving_percent = fields.Float('Saving (%)', digits=(5, 1), compute='_compute_cost_analysis')
    current_price_kwh = fields.Float('Average price at current times (per kWh)', digits=(12, 4),
                                     compute='_compute_cost_analysis')
    proposed_price_kwh = fields.Float('Average proposed price (per kWh)', digits=(12, 4),
                                      compute='_compute_cost_analysis')
    cost_breakdown = fields.Html('Cost by work center', sanitize=False, compute='_compute_cost_analysis')

    def _window_zone(self):
        return pytz.timezone(self.env.context.get('tz') or self.env.user.tz or 'UTC')

    def _local_window_part(self, value, is_end):
        """Split a stored UTC datetime into the local day and quarter-hour shown on the form."""
        if not value:
            return False, False
        local = pytz.utc.localize(value).astimezone(self._window_zone()).replace(tzinfo=None)
        if is_end and local.time() == time.min:
            # Show a window ending at midnight as 24:00 on the previous day.
            return (local - timedelta(days=1)).date(), '24:00'
        minute = local.hour * 60 + local.minute
        quarter = minute - minute % 15
        return local.date(), f'{quarter // 60:02d}:{quarter % 60:02d}'

    # Start and end are computed separately, so choosing one never clears the other.
    @api.depends('date_from')
    @api.depends_context('tz')
    def _compute_window_start(self):
        for run in self:
            run.window_start_date, run.window_start_time = run._local_window_part(run.date_from, False)

    @api.depends('date_to')
    @api.depends_context('tz')
    def _compute_window_end(self):
        for run in self:
            run.window_end_date, run.window_end_time = run._local_window_part(run.date_to, True)

    @api.onchange('window_start_date', 'window_end_date', 'window_start_time', 'window_end_time')
    def _onchange_window_parts(self):
        zone = self._window_zone()
        for run in self:
            for day, clock, target in ((run.window_start_date, run.window_start_time, 'date_from'),
                                       (run.window_end_date, run.window_end_time, 'date_to')):
                if not (day and clock):
                    continue
                hours, minutes = map(int, clock.split(':'))
                local = datetime.combine(day, time.min) + timedelta(hours=hours, minutes=minutes)
                run[target] = _utc(zone.localize(local))

    @api.depends('method')
    def _compute_method_label(self):
        labels = {
            METHOD: _('Greedy scheduling'),
            BASELINE_METHOD: _('Improved from existing bookings'),
            EXACT_METHOD: _('Best schedule across the available start times'),
            BOUNDED_METHOD: _('Best schedule found before the search limit'),
        }
        for run in self:
            run.method_label = labels.get(run.method, run.method)

    @api.depends('currency_id', 'line_ids.status', 'line_ids.energy_kwh', 'line_ids.current_cost',
                 'line_ids.proposed_cost', 'line_ids.has_current_cost', 'line_ids.workcenter_id')
    def _compute_cost_analysis(self):
        for run in self:
            scheduled = run.line_ids.filtered(lambda line: line.status != 'unscheduled')
            # Only operations with a priced current booking can be compared like for like.
            compared = scheduled.filtered('has_current_cost')
            current = sum(compared.mapped('current_cost'))
            proposed = sum(compared.mapped('proposed_cost'))
            compared_kwh = sum(compared.mapped('energy_kwh'))
            scheduled_kwh = sum(scheduled.mapped('energy_kwh'))
            run.energy_kwh = scheduled_kwh
            run.compared_count = len(compared)
            run.compared_current_cost = current
            run.compared_proposed_cost = proposed
            run.compared_saving = current - proposed
            run.saving_percent = 100 * (current - proposed) / current if current else 0
            run.current_price_kwh = current / compared_kwh if compared_kwh else 0
            run.proposed_price_kwh = sum(scheduled.mapped('proposed_cost')) / scheduled_kwh if scheduled_kwh else 0
            run.cost_breakdown = run._cost_breakdown_table(scheduled) if scheduled else False

    def _cost_breakdown_table(self, lines):
        self.ensure_one()

        def money(value):
            return format_amount(self.env, value, self.currency_id) if self.currency_id else f'{value:.2f}'

        labels = [_('Work center'), _('Operations'), _('kWh'), _('At current times'), _('Proposed'),
                  _('Saving'), _('Saving %')]

        def row(values):
            # data-label lets narrow screens show each row as a labelled card instead of scrolling sideways.
            cells = [Markup('<td data-label="{}">{}</td>').format(labels[0], values[0])]
            cells += [Markup('<td class="text-end" data-label="{}">{}</td>').format(label, value)
                      for label, value in zip(labels[1:], values[1:])]
            return Markup('<tr>{}</tr>').format(Markup('').join(cells))

        rows = []
        for center in lines.workcenter_id.sorted('name'):
            group = lines.filtered(lambda line: line.workcenter_id == center)
            compared = group.filtered('has_current_cost')
            kwh = sum(group.mapped('energy_kwh'))
            proposed = sum(group.mapped('proposed_cost'))
            if compared and len(compared) == len(group):
                current = sum(compared.mapped('current_cost'))
                saving = current - proposed
                percent = f'{100 * saving / current:.1f}%' if current else '-'
                current_text, saving_text = money(current), money(saving)
            else:
                current_text = saving_text = percent = '-'
            rows.append(row([center.name, len(group), f'{kwh:,.0f}', current_text, money(proposed),
                             saving_text, percent]))
        headings = [Markup('<th>{}</th>').format(labels[0])]
        headings += [Markup('<th class="text-end">{}</th>').format(label) for label in labels[1:]]
        header = Markup(
            '<table class="table table-sm o_gridsync_cost_table"><thead><tr>{}</tr></thead><tbody>'
        ).format(Markup('').join(headings))
        return header + Markup('').join(rows) + Markup('</tbody></table>')

    @api.depends('proposed_cost', 'current_cost', 'lower_bound', 'has_current_cost', 'has_lower_bound')
    def _compute_comparison(self):
        for run in self:
            run.saving = run.current_cost - run.proposed_cost if run.has_current_cost else 0
            run.gap = run.proposed_cost - run.lower_bound if run.has_lower_bound else 0

    @api.model_create_multi
    def create(self, values_list):
        if not self.env.su:
            if any(RESULT_FIELDS.intersection(values) for values in values_list):
                raise AccessError(_('Proposal results are set by GridSync.'))
            draft = dict.fromkeys(RESULT_FIELDS, False)
            draft.update(state='draft', line_ids=[])
            values_list = [dict(values, **draft) for values in values_list]
        return super().create(values_list)

    def write(self, values):
        if not self.env.su:
            if RESULT_FIELDS.intersection(values):
                raise AccessError(_('Proposal results are set by GridSync.'))
            if values.get('company_id') and values['company_id'] not in self.env.companies.ids:
                raise AccessError(_('The selected company is not available to this user.'))
            if INPUT_FIELDS.intersection(values):
                self.check_access('write')
                if self.try_lock_for_update(allow_referencing=True) != self:
                    raise UserError(_('A proposal is being updated. Try again shortly.'))
                self.invalidate_recordset(['state'])
                if self.filtered(lambda run: run.state != 'draft'):
                    raise UserError(_('Start a new run to change the proposal settings.'))
        return super().write(values)

    def action_new_run(self):
        self.ensure_one()
        run = self.copy()
        return {
            'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': run.id,
            'view_mode': 'form', 'target': 'current',
        }

    def action_compute(self):
        self.ensure_one()
        self.check_access('write')
        if not self.env.user.has_group(GRID_PLANNER):
            raise AccessError(_('Only GridSync planners can compute proposals.'))
        if not self.try_lock_for_update(allow_referencing=True):
            raise UserError(_('This proposal is being updated. Try again shortly.'))
        self.invalidate_recordset(['state'])
        if self.state != 'draft':
            raise UserError(_('This proposal has already been submitted. Start a new run instead.'))
        return self._compute_proposal(allow_queue=True)

    def _compute_proposal(self, *, allow_queue=False):
        now = fields.Datetime.now()
        start, end = ceil_slot(max(self.date_from, now)), floor_slot(self.date_to)
        if end <= start:
            raise UserError(_('The period must end after the next 15-minute mark.'))
        if end - start > MAX_HORIZON:
            raise UserError(_('Choose a period of up to 14 days.'))
        centers = self._check_workcenters()
        workorders = self.env['mrp.workorder'].search([
            ('workcenter_id', 'in', centers.ids), ('company_id', '=', self.company_id.id),
            ('state', 'in', ('ready', 'blocked')),
            ('production_id.state', 'in', ('confirmed', 'progress')),
            '|', ('date_start', '=', False), '&', ('date_start', '>=', start), ('date_start', '<', end),
        ], order='id', limit=MAX_JOBS + 1)
        if len(workorders) > MAX_JOBS:
            raise UserError(_('Choose fewer work centers or a shorter period. A run can include up to %s operations.', MAX_JOBS))
        if allow_queue and len(workorders) > self.company_id.sudo().grid_queue_threshold:
            self.sudo().write({'state': 'queued', 'queued_by': self.env.uid})
            self.env.ref('mrp_grid_scheduler.ir_cron_grid_schedule_queue').sudo()._trigger()
            return True
        center_inputs, notes, sources, components = self._workcenter_inputs(centers, workorders, start, end)
        job_inputs = self._job_inputs(workorders, start)
        try:
            result = schedule(start, end, center_inputs, job_inputs)
        except ScheduleInputError as exc:
            raise UserError(str(exc)) from exc
        self._save_result(start, end, now, workorders, center_inputs, job_inputs, result, notes, sources, components)
        return True

    @api.model
    def _cron_compute_queue(self):
        cron = self.env['ir.cron']
        queue = self.sudo()
        domain = [('state', '=', 'queued')]
        for run in queue.search(domain, order='id', limit=4):
            if not run.try_lock_for_update(allow_referencing=True):
                continue
            run.invalidate_recordset(['state'])
            if run.state != 'queued':
                continue
            run._compute_queued_proposal()
            if not cron._commit_progress(1, remaining=queue.search_count(domain)):
                break

    def _compute_queued_proposal(self):
        self.ensure_one()
        planner = self.queued_by
        company = self.company_id
        try:
            with self.env.cr.savepoint():
                if (not planner.active or company not in planner.company_ids
                        or not planner.has_group(GRID_PLANNER)):
                    raise AccessError(_('The requesting planner no longer has access to this company.'))
                run = self.with_user(planner).with_context({}, allowed_company_ids=[company.id])
                run.check_access('write')
                run._compute_proposal()
        except (UserError, AccessError) as exc:
            self.write({'state': 'failed', 'compute_error': str(exc)[:500]})
        except OperationalError:
            raise
        except Exception:
            _logger.exception('Could not compute queued GridSync proposal %s', self.id)
            self.write({'state': 'failed', 'compute_error': _('Computation failed. Check the Odoo log.')})

    def _check_workcenters(self, require_tariff=True):
        centers = self.workcenter_ids
        if not centers:
            raise UserError(_('Choose at least one work center.'))
        for center in centers:
            tariff = center.grid_tariff_source_id
            if not center.active:
                raise UserError(_('The work center %s is archived.', center.name))
            if require_tariff and (not center.grid_allow_shift or center.grid_power_kw <= 0 or (self.objective != 'gb_carbon' and not tariff.active)):
                raise UserError(_('Turn on schedule suggestions for %s and give it power and an active tariff.', center.name))
            if center.company_id != self.company_id or (require_tariff and self.objective != 'gb_carbon' and tariff.company_id != self.company_id):
                raise UserError(_('The work center and tariff for %s must belong to the proposal company.', center.name))
            if require_tariff and self.objective != 'cost' and not center.grid_use_gb_carbon:
                raise UserError(_('Confirm that %s uses the Great Britain electricity grid on its Electricity tab.', center.name))
            calendar = center.resource_calendar_id
            if not calendar:
                raise UserError(_('Add working hours to %s before scheduling it.', center.name))
            if calendar.flexible_hours:
                raise UserError(_('%s uses flexible hours. Choose fixed working hours before scheduling it.', center.name))
        return centers

    def _tariff_exchange_rates(self, tariffs, date):
        currencies = tariffs.currency_id
        if len(currencies) <= 1:
            return {}
        company = self.company_id
        rates = self.env['res.currency.rate'].sudo()
        exchange = {}
        for currency in currencies - company.currency_id:
            domain = [('currency_id', '=', currency.id), ('name', '<=', date)]
            rate = rates.search(domain + [('company_id', '=', company.root_id.id)], order='name desc', limit=1)
            if not rate:
                rate = rates.search(domain + [('company_id', '=', False)], order='name desc', limit=1)
            if not rate or not math.isfinite(rate.rate) or rate.rate <= 0:
                raise UserError(_('Add an Odoo exchange rate for %(currency)s on or before %(date)s.',
                                  currency=currency.name, date=date))
            factor = currency._get_conversion_rate(currency, company.currency_id, company, date)
            if not math.isfinite(factor) or factor <= 0:
                raise UserError(_('The exchange rate for %s is outside the supported range.', currency.name))
            exchange[currency.id] = {'factor': factor, 'date': rate.name.isoformat()}
        return exchange

    def _gb_carbon_inputs(self, start, end):
        first = start.replace(minute=start.minute // 30 * 30)
        intervals = self.env['grid.carbon.interval'].search_fetch([
            ('company_id', '=', self.company_id.id),
            ('start_utc', '>=', first), ('start_utc', '<', end),
        ], ['start_utc', 'actual', 'forecast', 'has_actual', 'has_forecast'])
        factors, basis = {}, {}
        for interval in intervals:
            if not (interval.has_actual or interval.has_forecast):
                continue
            for slot in (interval.start_utc, interval.start_utc + STEP):
                if start <= slot < end:
                    factors[slot] = interval.actual if interval.has_actual else interval.forecast
                    basis[slot.isoformat()] = 'published_estimate' if interval.has_actual else 'forecast'
        return factors, basis

    def _workcenter_inputs(self, centers, workorders, start, end, include_prices=True):
        combined = include_prices and self.objective == 'gb_combined'
        use_carbon = include_prices and self.objective != 'cost'
        tariffs = centers.grid_tariff_source_id if include_prices and self.objective != 'gb_carbon' else self.env['grid.tariff.source']
        if combined and (not math.isfinite(self.carbon_price) or not 0 < self.carbon_price <= 1_000_000):
            raise UserError(_('Enter a carbon price above zero and up to 1,000,000 per tonne.'))
        if combined and any(tariff.currency_id != self.company_id.currency_id or tariff.source_type == 'energy_charts'
                            for tariff in tariffs):
            raise UserError(_('Use a Great Britain site tariff in the company currency for each work center.'))
        carbon, carbon_basis = self._gb_carbon_inputs(start, end) if use_carbon else ({}, {})
        slots = self.env['grid.tariff.slot'].search([
            ('source_id', 'in', tariffs.ids), ('is_current', '=', True),
            ('start_utc', '>=', start), ('start_utc', '<', end),
        ])
        exchange = self._tariff_exchange_rates(tariffs, start.date())
        prices = {tariff.id: {} for tariff in tariffs}
        for slot in slots:
            rate = exchange.get(slot.source_id.currency_id.id)
            prices[slot.source_id.id][slot.start_utc] = slot.price_per_mwh * rate['factor'] if rate else slot.price_per_mwh
        # Only booking times on the selected company's resources are needed.
        leave_company = ('company_id', 'in', [False, self.company_id.id])
        leaves = self.env['resource.calendar.leaves'].sudo().search_fetch([
            ('time_type', '=', 'other'), leave_company,
            ('date_from', '<', end), ('date_to', '>', start),
            ('id', 'not in', workorders.leave_id.ids),
            '|', ('resource_id', 'in', centers.resource_id.ids),
            '&', ('resource_id', '=', False), ('calendar_id', 'in', centers.resource_calendar_id.ids),
        ], ['date_from', 'date_to', 'calendar_id', 'resource_id'])
        inputs, components = [], {}
        for center in centers:
            calendar = center.resource_calendar_id
            intervals = calendar._work_intervals_batch(
                pytz.utc.localize(start), pytz.utc.localize(end), resources=center.resource_id,
                domain=[('time_type', '=', 'leave'), leave_company], tz=pytz.timezone(calendar.tz or 'UTC'),
            )[center.resource_id.id]
            busy = [(leave.date_from, leave.date_to) for leave in leaves if leave.resource_id == center.resource_id or (
                not leave.resource_id and leave.calendar_id == calendar)]
            tariff_prices = prices.get(center.grid_tariff_source_id.id, {})
            if combined:
                components[center.id] = (tariff_prices, carbon)
                scores = {slot: price + carbon[slot] * self.carbon_price / 1000
                          for slot, price in tariff_prices.items() if slot in carbon}
            else:
                scores = carbon if use_carbon else tariff_prices
            inputs.append(WorkcenterInput(
                key=center.id, power_kw=center.grid_power_kw,
                working=tuple((_utc(low), _utc(high)) for low, high, _records in intervals),
                busy=tuple(sorted(busy)), prices=scores,
            ))
        expected = (end - start) // STEP
        notes, sources = [], []
        if use_carbon:
            notes.append(_('NESO Great Britain national grid, CC BY 4.0. %(found)s of %(expected)s quarter-hours covered; %(forecast)s use a forecast.',
                           found=len(carbon), expected=expected, forecast=list(carbon_basis.values()).count('forecast')))
            sources.append({'id': 'neso-gb', 'currency': False, 'carbon_basis': carbon_basis,
                            **({'carbon_factors': {slot.isoformat(): factor for slot, factor in carbon.items()}} if combined else {})})
            if combined:
                notes.append(_('Carbon price: %(price)s %(currency)s per tonne of CO₂.',
                               price=self.carbon_price, currency=self.company_id.currency_id.name))
        for tariff in tariffs:
            found = len(prices[tariff.id])
            label = dict(tariff._fields['source_type']._description_selection(self.env))[tariff.source_type]
            line = _('%(name)s (%(type)s, %(currency)s): %(found)s of %(expected)s price intervals in this period.',
                     name=tariff.name, type=label, currency=tariff.currency_id.name, found=found, expected=expected)
            if tariff.last_sync_at:
                line += ' ' + _('Last loaded %s UTC.', tariff.last_sync_at)
            if tariff.source_type == 'energy_charts':
                line += ' ' + _('Published day-ahead wholesale prices, not a supply bill.')
            if tariff.source_type == 'csv':
                basis = dict(tariff._fields['csv_price_basis']._description_selection(self.env))[tariff.csv_price_basis]
                line += ' ' + _('Price type: %s.', basis)
            if tariff.profile_dirty:
                line += ' ' + _('Rules changed after the last generation; affected prices are not used.')
            if tariff.source_reference:
                line += ' ' + _('Source: %s', tariff.source_reference)
            rate = exchange.get(tariff.currency_id.id)
            if rate:
                line += ' ' + _('Exchange rate: 1 %(source)s = %(factor)s %(target)s, dated %(date)s.',
                                 source=tariff.currency_id.name, factor=format(rate['factor'], '.12g'),
                                 target=self.company_id.currency_id.name, date=rate['date'])
            notes.append(line)
            sources.append({
                'id': tariff.id, 'name': tariff.name, 'type': tariff.source_type, 'currency': tariff.currency_id.name,
                'reference': tariff.source_reference, 'attribution': tariff.attribution,
                'last_sync_at': tariff.last_sync_at and tariff.last_sync_at.isoformat(),
                'generated_at': tariff.generated_at and tariff.generated_at.isoformat(),
                'intervals': found, 'expected_intervals': expected,
                **({'exchange_rate': rate, 'converted_to': self.company_id.currency_id.name} if rate else {}),
            })
        for center in centers:
            notes.append(_('%(name)s: %(power)s kW (%(reference)s), working hours %(calendar)s.',
                           name=center.name, power=center.grid_power_kw,
                           reference=center.grid_power_reference or _('no reference'),
                           calendar=center.resource_calendar_id.name))
        return inputs, notes, sources, components

    def _job_inputs(self, workorders, start, include_penalties=True):
        keys = set(workorders.ids)
        jobs = []
        centers = self.workcenter_ids
        penalties = {}
        if include_penalties and self.objective != 'gb_carbon':
            configured = centers.filtered('grid_shift_penalty')
            if configured:
                rates = self._tariff_exchange_rates(centers.grid_tariff_source_id, start.date())
                penalties = {
                    center.id: center.grid_shift_penalty * rates.get(center.grid_tariff_source_id.currency_id.id, {}).get('factor', 1)
                    for center in configured
                }
        for workorder in workorders:
            release, due, blocked, predecessors = start, workorder.production_id.date_deadline or None, None, []
            for predecessor in workorder.blocked_by_workorder_ids:
                if predecessor.state in ('done', 'cancel'):
                    continue
                if predecessor.id in keys:
                    predecessors.append(predecessor.id)
                elif predecessor.date_finished:
                    release = max(release, predecessor.date_finished)
                else:
                    blocked = 'external_unplanned'
            for successor in workorder.needed_by_workorder_ids:
                if successor.state not in ('done', 'cancel') and successor.id not in keys and successor.date_start:
                    due = min(due, successor.date_start) if due else successor.date_start
            jobs.append(JobInput(
                key=workorder.id, workcenter=workorder.workcenter_id.id, minutes=workorder.duration_expected,
                release=release, due=due, predecessors=tuple(sorted(predecessors)),
                current_start=workorder.date_start or None, current_end=workorder.date_finished or None,
                blocked_reason=blocked, shift_penalty_per_hour=penalties.get(workorder.workcenter_id.id, 0),
            ))
        return jobs

    def _combined_amounts(self, placement, center, components):
        if not placement:
            return 0, 0
        tariffs, carbon = components
        cost = emissions = 0
        for start, end in center.working:
            start, end = max(start, placement.start), min(end, placement.end)
            if start >= end:
                continue
            slot = floor_slot(start)
            while slot < end:
                hours = (min(end, slot + STEP) - max(start, slot)).total_seconds() / 3600
                kwh = center.power_kw * hours
                cost += kwh * tariffs[slot] / 1000
                emissions += kwh * carbon[slot] / 1000
                slot += STEP
        return cost, emissions

    def _save_result(self, start, end, now, workorders, center_inputs, job_inputs, result, notes, sources, components):
        use_carbon = self.objective == 'gb_carbon'
        combined = self.objective == 'gb_combined'
        centers = {center.key: center for center in center_inputs}
        jobs = {job.key: job for job in job_inputs}
        lines = []
        for workorder in workorders:
            item = result.jobs[workorder.id]
            reason = _reason_text(self.env, item.status, item.reason)
            if use_carbon and item.status != 'unscheduled':
                reason = _('Compared using estimated electricity emissions.')
            elif (use_carbon or combined) and item.reason == 'no_prices':
                reason = _('The data required for this objective is missing for every suitable time.')
            if item.status == 'unscheduled' and workorder.date_start:
                reason += ' ' + _('It keeps its current time.')
            penalty = item.proposed.shift_penalty if item.proposed else 0
            proposed_cost, current_cost = (item.proposed.cost - penalty if item.proposed else 0,
                                           item.current.cost if item.current else 0)
            proposed_carbon = current_carbon = 0
            if combined:
                center = centers[workorder.workcenter_id.id]
                proposed_cost, proposed_carbon = self._combined_amounts(item.proposed, center, components[center.key])
                current_cost, current_carbon = self._combined_amounts(item.current, center, components[center.key])
            elif use_carbon:
                proposed_carbon, current_carbon = proposed_cost, current_cost
                proposed_cost = current_cost = 0
            lines.append({
                'run_id': self.id, 'workorder_id': workorder.id, 'production_id': workorder.production_id.id,
                'workcenter_id': workorder.workcenter_id.id, 'operation': workorder.name,
                'tariff_source_id': workorder.workcenter_id.grid_tariff_source_id.id,
                'duration_minutes': workorder.duration_expected, 'deadline': jobs[workorder.id].due,
                'current_start': workorder.date_start, 'current_end': workorder.date_finished,
                'proposed_start': item.proposed and item.proposed.start,
                'proposed_end': item.proposed and item.proposed.end,
                'energy_kwh': item.proposed.energy_kwh if item.proposed else 0,
                'proposed_cost': proposed_cost, 'proposed_carbon_kg': proposed_carbon, 'shift_penalty': penalty,
                'current_cost': current_cost, 'current_carbon_kg': current_carbon,
                'has_current_cost': bool(item.current), 'status': item.status, 'reason_code': item.reason,
                'reason': reason,
            })
        self.env['grid.schedule.line'].sudo().create(lines)
        shift_penalty = math.fsum(line['shift_penalty'] for line in lines)
        has_shift_penalties = any(job.shift_penalty_per_hour for job in job_inputs)
        if has_shift_penalties:
            notes.append(_('The search includes estimated shift penalties. Energy costs and savings cover electricity only.'))
        statuses = [item.status for item in result.jobs.values()]
        if not workorders:
            notes.insert(0, _('No open operations were found on these work centers in this period.'))
        self.sudo().write({
            'state': 'proposed', 'computed_at': now, 'method': result.method,
            'currency_id': (self.company_id.currency_id if use_carbon or any(source.get('exchange_rate') for source in sources)
                            else self.workcenter_ids.grid_tariff_source_id.currency_id).id,
            'proposed_cost': math.fsum(line['proposed_cost'] for line in lines),
            'current_cost': (sum(line['current_cost'] for line in lines) if result.current_cost is not None else 0)
                            if combined else (0 if use_carbon else result.current_cost or 0),
            'proposed_carbon_kg': sum(line['proposed_carbon_kg'] for line in lines),
            'current_carbon_kg': sum(line['current_carbon_kg'] for line in lines) if result.current_cost is not None else 0,
            'carbon_lower_bound_kg': (result.lower_bound or 0) if use_carbon else 0,
            'shift_penalty': shift_penalty,
            'has_current_cost': result.current_cost is not None,
            'lower_bound': 0 if use_carbon or has_shift_penalties else result.lower_bound or 0,
            'has_lower_bound': not combined and not has_shift_penalties and result.lower_bound is not None,
            'complete': result.complete, 'job_count': len(statuses),
            'moved_count': statuses.count('moved'), 'unchanged_count': statuses.count('unchanged'),
            'unscheduled_count': statuses.count('unscheduled'), 'data_notes': '\n'.join(notes),
            'snapshot': {
                'computed_at': now.isoformat(), 'requested_period': [self.date_from.isoformat(), self.date_to.isoformat()],
                'workorders': self._snapshot_workorders(workorders),
                'manufacturing': self._snapshot_manufacturing(workorders.production_id),
                'objective': self.objective, 'carbon_price': self.carbon_price if combined else None, 'sources': sources, 'inputs': dump_inputs(start, end, center_inputs, job_inputs),
                'result': {str(key): {
                    'status': item.status, 'reason': item.reason,
                    'start': item.proposed and item.proposed.start.isoformat(),
                    'end': item.proposed and item.proposed.end.isoformat(),
                    ('objective_score' if combined or has_shift_penalties else 'carbon_kg' if use_carbon else 'cost'):
                        item.proposed and item.proposed.cost,
                } for key, item in result.jobs.items()},
            },
        })


class GridScheduleLine(models.Model):
    _name = 'grid.schedule.line'
    _description = 'GridSync Proposed Operation'
    _order = 'run_id, proposed_start, current_start, id'

    run_id = fields.Many2one('grid.schedule.run', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='run_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='run_id.currency_id', store=True)
    workorder_id = fields.Many2one('mrp.workorder', string='Operation', ondelete='set null', index=True)
    production_id = fields.Many2one('mrp.production', string='Manufacturing order', ondelete='set null')
    workcenter_id = fields.Many2one('mrp.workcenter', string='Work center', ondelete='set null')
    operation = fields.Char('Operation name')
    duration_minutes = fields.Float('Expected minutes', digits=(16, 2))
    deadline = fields.Datetime('Finish by')
    current_start = fields.Datetime()
    current_end = fields.Datetime()
    proposed_start = fields.Datetime()
    proposed_end = fields.Datetime()
    energy_kwh = fields.Float('Estimated energy (kWh)', digits=(16, 3))
    current_cost = fields.Monetary('Cost at current time')
    proposed_cost = fields.Monetary('Proposed cost')
    shift_penalty = fields.Monetary('Estimated shift penalty')
    objective = fields.Selection(related='run_id.objective')
    proposed_carbon_kg = fields.Float('Proposed kg CO₂', digits=(20, 6))
    current_carbon_kg = fields.Float('Current kg CO₂', digits=(20, 6))
    has_current_cost = fields.Boolean()
    saving = fields.Monetary(compute='_compute_saving', store=True,
                             help='Cost at the current time minus the proposed cost, when both are known.')
    status = fields.Selection([
        ('moved', 'Moved'), ('unchanged', 'Unchanged'), ('unscheduled', 'Not scheduled'),
    ], required=True)
    reason_code = fields.Char()
    reason = fields.Char()

    @api.depends('status', 'has_current_cost', 'current_cost', 'proposed_cost')
    def _compute_saving(self):
        for line in self:
            known = line.has_current_cost and line.status != 'unscheduled'
            line.saving = line.current_cost - line.proposed_cost if known else 0
