import math
from datetime import timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..services.scheduler import (
    BOUNDED_METHOD, EXACT_METHOD, MAX_HORIZON, MAX_JOBS, METHOD, STEP, JobInput, ScheduleInputError, WorkcenterInput,
    ceil_slot, dump_inputs, floor_slot, schedule,
)


GRID_PLANNER = 'mrp_grid_scheduler.group_grid_planner'
INPUT_FIELDS = {'company_id', 'date_from', 'date_to', 'workcenter_ids', 'objective'}
RESULT_FIELDS = {
    'state', 'computed_at', 'method', 'currency_id', 'proposed_cost', 'current_cost', 'lower_bound',
    'has_current_cost', 'has_lower_bound', 'complete',
    'proposed_carbon_kg', 'current_carbon_kg', 'carbon_lower_bound_kg', 'job_count', 'moved_count', 'unchanged_count',
    'unscheduled_count', 'data_notes', 'snapshot', 'line_ids',
    'applied_at', 'applied_by', 'reverted_at', 'reverted_by', 'application_snapshot',
}


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

    def _default_date_from(self):
        return ceil_slot(fields.Datetime.now())

    def _default_date_to(self):
        return ceil_slot(fields.Datetime.now()) + timedelta(days=1)

    name = fields.Char(required=True, default=lambda self: self.env._('Schedule proposal'))
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('proposed', 'Proposed'), ('applied', 'Applied'), ('reverted', 'Reverted'),
    ], default='draft', required=True, readonly=True, copy=False)
    date_from = fields.Datetime('Period start', required=True, default=_default_date_from)
    date_to = fields.Datetime('Period end', required=True, default=_default_date_to)
    workcenter_ids = fields.Many2many(
        'mrp.workcenter', string='Work centers', check_company=True,
        domain="[('company_id', '=', company_id), ('grid_allow_shift', '=', True)]",
    )
    objective = fields.Selection([
        ('cost', 'Electricity cost'), ('gb_carbon', 'GB electricity emissions'),
    ], required=True, default='cost')
    proposed_carbon_kg = fields.Float('Proposed emissions (kg CO₂)', digits=(20, 6), readonly=True, copy=False)
    current_carbon_kg = fields.Float('Current emissions (kg CO₂)', digits=(20, 6), readonly=True, copy=False)
    carbon_lower_bound_kg = fields.Float('Emissions lower bound (kg CO₂)', digits=(20, 6), readonly=True, copy=False)
    computed_at = fields.Datetime(readonly=True, copy=False)
    method = fields.Char(readonly=True, copy=False)
    method_label = fields.Char(string="Search result", compute="_compute_method_label")
    currency_id = fields.Many2one('res.currency', readonly=True, copy=False)
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
    application_snapshot = fields.Json(readonly=True, copy=False)
    applied_at = fields.Datetime(readonly=True, copy=False)
    applied_by = fields.Many2one('res.users', readonly=True, copy=False)
    reverted_at = fields.Datetime(readonly=True, copy=False)
    reverted_by = fields.Many2one('res.users', readonly=True, copy=False)
    line_ids = fields.One2many('grid.schedule.line', 'run_id', string='Proposed operations', readonly=True, copy=False)

    @api.depends('method')
    def _compute_method_label(self):
        labels = {
            METHOD: _('Greedy scheduling'),
            EXACT_METHOD: _('Best schedule across the available start times'),
            BOUNDED_METHOD: _('Best schedule found before the search limit'),
        }
        for run in self:
            run.method_label = labels.get(run.method, run.method)

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
            if INPUT_FIELDS.intersection(values) and self.filtered(lambda run: run.state != 'draft'):
                raise UserError(_('Start a new run to change the period or work centers.'))
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
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(%s, %s)', (5108845, self.id))
        if not self.env.cr.fetchone()[0] or self.state != 'draft':
            raise UserError(_('This proposal has already been computed. Start a new run instead.'))
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
        center_inputs, notes, sources = self._workcenter_inputs(centers, workorders, start, end)
        job_inputs = self._job_inputs(workorders, start)
        try:
            result = schedule(start, end, center_inputs, job_inputs)
        except ScheduleInputError as exc:
            raise UserError(str(exc)) from exc
        self._save_result(start, end, now, workorders, center_inputs, job_inputs, result, notes, sources)
        return True

    def _check_workcenters(self, require_tariff=True):
        centers = self.workcenter_ids
        if not centers:
            raise UserError(_('Choose at least one work center.'))
        for center in centers:
            tariff = center.grid_tariff_source_id
            if not center.active:
                raise UserError(_('The work center %s is archived.', center.name))
            if require_tariff and (not center.grid_allow_shift or center.grid_power_kw <= 0 or (self.objective == 'cost' and not tariff.active)):
                raise UserError(_('Turn on schedule suggestions for %s and give it power and an active tariff.', center.name))
            if center.company_id != self.company_id or (require_tariff and self.objective == 'cost' and tariff.company_id != self.company_id):
                raise UserError(_('The work center and tariff for %s must belong to the proposal company.', center.name))
            if require_tariff and self.objective == 'gb_carbon' and not center.grid_use_gb_carbon:
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
        use_carbon = include_prices and self.objective == 'gb_carbon'
        tariffs = centers.grid_tariff_source_id if include_prices and not use_carbon else self.env['grid.tariff.source']
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
        inputs = []
        for center in centers:
            calendar = center.resource_calendar_id
            intervals = calendar._work_intervals_batch(
                pytz.utc.localize(start), pytz.utc.localize(end), resources=center.resource_id,
                domain=[('time_type', '=', 'leave'), leave_company], tz=pytz.timezone(calendar.tz or 'UTC'),
            )[center.resource_id.id]
            busy = [(leave.date_from, leave.date_to) for leave in leaves if leave.resource_id == center.resource_id or (
                not leave.resource_id and leave.calendar_id == calendar)]
            inputs.append(WorkcenterInput(
                key=center.id, power_kw=center.grid_power_kw,
                working=tuple((_utc(low), _utc(high)) for low, high, _records in intervals),
                busy=tuple(sorted(busy)), prices=carbon if use_carbon else prices.get(center.grid_tariff_source_id.id, {}),
            ))
        expected = (end - start) // STEP
        notes, sources = [], []
        if use_carbon:
            notes.append(_('NESO Great Britain national grid, CC BY 4.0. %(found)s of %(expected)s quarter-hours covered; %(forecast)s use a forecast.',
                           found=len(carbon), expected=expected, forecast=list(carbon_basis.values()).count('forecast')))
            sources.append({'id': 'neso-gb', 'currency': False, 'carbon_basis': carbon_basis})
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
        return inputs, notes, sources

    def _job_inputs(self, workorders, start):
        keys = set(workorders.ids)
        jobs = []
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
                blocked_reason=blocked,
            ))
        return jobs

    def _save_result(self, start, end, now, workorders, center_inputs, job_inputs, result, notes, sources):
        use_carbon = self.objective == 'gb_carbon'
        jobs = {job.key: job for job in job_inputs}
        lines = []
        for workorder in workorders:
            item = result.jobs[workorder.id]
            reason = _reason_text(self.env, item.status, item.reason)
            if use_carbon and item.status != 'unscheduled':
                reason = _('Compared using estimated electricity emissions.')
            elif use_carbon and item.reason == 'no_prices':
                reason = _('Carbon intensity is missing for every suitable time.')
            if item.status == 'unscheduled' and workorder.date_start:
                reason += ' ' + _('It keeps its current time.')
            lines.append({
                'run_id': self.id, 'workorder_id': workorder.id, 'production_id': workorder.production_id.id,
                'workcenter_id': workorder.workcenter_id.id, 'operation': workorder.name,
                'tariff_source_id': workorder.workcenter_id.grid_tariff_source_id.id,
                'duration_minutes': workorder.duration_expected, 'deadline': jobs[workorder.id].due,
                'current_start': workorder.date_start, 'current_end': workorder.date_finished,
                'proposed_start': item.proposed and item.proposed.start,
                'proposed_end': item.proposed and item.proposed.end,
                'energy_kwh': item.proposed.energy_kwh if item.proposed else 0,
                'proposed_cost': item.proposed.cost if item.proposed and not use_carbon else 0,
                'proposed_carbon_kg': item.proposed.cost if item.proposed and use_carbon else 0,
                'current_cost': item.current.cost if item.current and not use_carbon else 0,
                'current_carbon_kg': item.current.cost if item.current and use_carbon else 0,
                'has_current_cost': bool(item.current), 'status': item.status, 'reason_code': item.reason,
                'reason': reason,
            })
        self.env['grid.schedule.line'].sudo().create(lines)
        statuses = [item.status for item in result.jobs.values()]
        if not workorders:
            notes.insert(0, _('No open operations were found on these work centers in this period.'))
        self.sudo().write({
            'state': 'proposed', 'computed_at': now, 'method': result.method,
            'currency_id': (self.company_id.currency_id if use_carbon or any(source.get('exchange_rate') for source in sources)
                            else self.workcenter_ids.grid_tariff_source_id.currency_id).id,
            'proposed_cost': 0 if use_carbon else result.proposed_cost,
            'current_cost': 0 if use_carbon else result.current_cost or 0,
            'proposed_carbon_kg': result.proposed_cost if use_carbon else 0,
            'current_carbon_kg': (result.current_cost or 0) if use_carbon else 0,
            'carbon_lower_bound_kg': (result.lower_bound or 0) if use_carbon else 0,
            'has_current_cost': result.current_cost is not None,
            'lower_bound': 0 if use_carbon else result.lower_bound or 0, 'has_lower_bound': result.lower_bound is not None,
            'complete': result.complete, 'job_count': len(statuses),
            'moved_count': statuses.count('moved'), 'unchanged_count': statuses.count('unchanged'),
            'unscheduled_count': statuses.count('unscheduled'), 'data_notes': '\n'.join(notes),
            'snapshot': {
                'computed_at': now.isoformat(), 'requested_period': [self.date_from.isoformat(), self.date_to.isoformat()],
                'workorders': self._snapshot_workorders(workorders),
                'manufacturing': self._snapshot_manufacturing(workorders.production_id),
                'objective': self.objective, 'sources': sources, 'inputs': dump_inputs(start, end, center_inputs, job_inputs),
                'result': {str(key): {
                    'status': item.status, 'reason': item.reason,
                    'start': item.proposed and item.proposed.start.isoformat(),
                    'end': item.proposed and item.proposed.end.isoformat(),
                    ('carbon_kg' if use_carbon else 'cost'): item.proposed and item.proposed.cost,
                } for key, item in result.jobs.items()},
            },
        })


class GridScheduleLine(models.Model):
    _name = 'grid.schedule.line'
    _description = 'GridSync Proposed Operation'
    _order = 'run_id, proposed_start, current_start, id'

    run_id = fields.Many2one('grid.schedule.run', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='run_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='run_id.currency_id')
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
    objective = fields.Selection(related='run_id.objective')
    proposed_carbon_kg = fields.Float('Proposed kg CO₂', digits=(20, 6))
    current_carbon_kg = fields.Float('Current kg CO₂', digits=(20, 6))
    has_current_cost = fields.Boolean()
    status = fields.Selection([
        ('moved', 'Moved'), ('unchanged', 'Unchanged'), ('unscheduled', 'Not scheduled'),
    ], required=True)
    reason_code = fields.Char()
    reason = fields.Char()
