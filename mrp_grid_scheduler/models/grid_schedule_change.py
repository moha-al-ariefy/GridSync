from datetime import datetime

from psycopg2.errors import LockNotAvailable

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..services.scheduler import ScheduleInputError, check_placements, dump_inputs
from .grid_schedule import GRID_PLANNER


def _date(value):
    return value.isoformat() if value else False


class GridScheduleRun(models.Model):
    _inherit = 'grid.schedule.run'

    def _snapshot_workorders(self, workorders):
        return {str(order.id): {
            'write_date': _date(order.write_date),
            'name': order.name,
            'state': order.state, 'workcenter': order.workcenter_id.id,
            'production': order.production_id.id, 'duration': order.duration_expected,
            'start': _date(order.date_start), 'end': _date(order.date_finished),
            'booking': order.leave_id.id,
            'booking_write_date': _date(order.leave_id.write_date),
            'booking_start': _date(order.leave_id.date_from),
            'booking_end': _date(order.leave_id.date_to),
            'booking_calendar': order.leave_id.calendar_id.id,
            'booking_resource': order.leave_id.resource_id.id,
            'predecessors': sorted(order.blocked_by_workorder_ids.ids),
            'successors': sorted(order.needed_by_workorder_ids.ids),
        } for order in workorders}

    def _snapshot_manufacturing(self, productions):
        return {str(production.id): {
            'write_date': _date(production.write_date),
            'state': production.state, 'start': _date(production.date_start),
            'end': _date(production.date_finished), 'deadline': _date(production.date_deadline),
            'operations': sorted(production.workorder_ids.ids),
        } for production in productions}

    def _check_transition(self, operation):
        self.ensure_one()
        self.check_access('write')
        if not self.env.user.has_group(GRID_PLANNER):
            raise AccessError(_('Only GridSync planners can change schedules.'))
        if operation == 'apply':
            if self.state != 'proposed' or not self.complete or not self.moved_count:
                raise UserError(_('Choose a complete proposal with changes to apply.'))
            if not self.snapshot or not self.snapshot.get('workorders'):
                raise UserError(_('Compute a new proposal before applying this schedule.'))
        elif self.state != 'applied' or not self.application_snapshot:
            raise UserError(_('Only an applied proposal can be reverted.'))

    @api.ondelete(at_uninstall=False)
    def _keep_schedule_history(self):
        if any(run.state in ('applied', 'reverted') for run in self):
            raise UserError(_('Keep applied and reverted schedules in the history.'))

    def action_review_apply(self):
        return self._open_schedule_confirmation('apply')

    def action_review_revert(self):
        return self._open_schedule_confirmation('revert')

    def _open_schedule_confirmation(self, operation):
        self._check_transition(operation)
        action = self.env['ir.actions.actions']._for_xml_id('mrp_grid_scheduler.action_grid_schedule_confirm')
        action['name'] = _('Apply schedule') if operation == 'apply' else _('Revert schedule')
        action['context'] = {'default_run_id': self.id, 'default_operation': operation}
        return action

    def _confirm_schedule_change(self, operation):
        self._check_transition(operation)
        context = {
            'allowed_company_ids': self.env.companies.ids,
            'lang': self.env.lang,
            'tz': self.env.user.tz,
        }
        try:
            with self.env.registry.cursor() as cursor:
                cursor.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
                env = api.Environment(cursor, self.env.uid, context)
                run = env[self._name].browse(self.id)
                run._lock_schedule_inputs()
                run._change_schedule(operation)
        except LockNotAvailable as exc:
            raise UserError(self.env._('Scheduling records are being updated. Try again shortly.')) from exc
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    @api.model
    def _lock_schedule_inputs(self):
        """this one basicly keeps the booking write out inbetween validation and save"""
        self.env.cr.execute("""
            LOCK TABLE grid_schedule_run, mrp_production, mrp_workorder, resource_calendar_leaves
            IN SHARE ROW EXCLUSIVE MODE NOWAIT
        """)
        self.env.cr.execute("""
            LOCK TABLE mrp_workorder_dependencies_rel, mrp_workcenter, resource_resource, resource_calendar,
                       resource_calendar_attendance, grid_tariff_source, grid_tariff_slot
            IN SHARE MODE NOWAIT
        """)

    def _change_schedule(self, operation):
        self._check_transition(operation)
        self = self.with_company(self.company_id)
        lines = self.line_ids
        workorders = lines.workorder_id
        moved = lines.filtered(lambda line: line.status == 'moved')
        if len(workorders) != len(lines) or any(order.company_id != self.company_id for order in workorders):
            raise UserError(_('An operation is missing or belongs to another company. Compute a new proposal.'))
        moved.workorder_id.check_access('write')
        moved.workorder_id.production_id.check_access('write')
        guard = self.snapshot if operation == 'apply' else self.application_snapshot['after']
        before = self._snapshot_workorders(workorders)
        manufacturing = self._snapshot_manufacturing(workorders.production_id)
        for order in workorders:
            if before[str(order.id)] != guard['workorders'].get(str(order.id)):
                raise UserError(_('The work order %s changed. No dates were updated.', order.display_name))
        for production in workorders.production_id:
            if manufacturing[str(production.id)] != guard['manufacturing'].get(str(production.id)):
                raise UserError(_('The manufacturing order %s changed. No dates were updated.', production.display_name))
        if operation == 'apply' and self.objective == 'gb_carbon':
            self.env.cr.execute('LOCK TABLE grid_carbon_interval IN SHARE MODE NOWAIT')
        if operation == 'apply':
            self._apply_reviewed_schedule(lines, moved, workorders, before, manufacturing)
        else:
            self._revert_reviewed_schedule(moved, workorders)

    def _validate_schedule_dates(self, workorders, dates, start, end, require_prices):
        centers = self._check_workcenters(require_tariff=require_prices)
        inputs, _, sources = self._workcenter_inputs(centers, workorders, start, end, include_prices=require_prices)
        jobs = self._job_inputs(workorders, start)
        try:
            check_placements(start, end, inputs, jobs, dates, require_prices)
        except ScheduleInputError as exc:
            raise UserError(str(exc)) from exc
        return inputs, jobs, sources

    def _apply_reviewed_schedule(self, lines, moved, workorders, before, manufacturing):
        start, end = map(datetime.fromisoformat, self.snapshot['inputs']['horizon'])
        proposed = {line.workorder_id.id: (line.proposed_start, line.proposed_end) for line in lines}
        self._check_future_dates(moved, proposed)
        inputs, jobs, sources = self._validate_schedule_dates(workorders, proposed, start, end, True)
        source_keys = [(source['id'], source['currency'], source.get('exchange_rate'), source.get('converted_to'), source.get('carbon_basis'))
                       for source in sources]
        frozen_keys = [(source['id'], source['currency'], source.get('exchange_rate'), source.get('converted_to'), source.get('carbon_basis'))
                       for source in self.snapshot['sources']]
        if dump_inputs(start, end, inputs, jobs) != self.snapshot['inputs'] or source_keys != frozen_keys:
            if self.objective == 'gb_carbon':
                raise UserError(_('Carbon intensity, working hours or bookings changed. Compute a new proposal.'))
            raise UserError(_('Prices, exchange rates, working hours or bookings changed. Compute a new proposal.'))
        self._validate_restore_dates(workorders, before)
        self._write_schedule_dates(moved, proposed)
        self.sudo().write({
            'state': 'applied', 'applied_at': fields.Datetime.now(), 'applied_by': self.env.uid,
            'application_snapshot': {
                'before': {'workorders': before, 'manufacturing': manufacturing},
                'after': {
                    'workorders': self._snapshot_workorders(workorders),
                    'manufacturing': self._snapshot_manufacturing(workorders.production_id),
                },
            },
        })

    def _restore_dates(self, before):
        return {int(key): (datetime.fromisoformat(value['start']), datetime.fromisoformat(value['end']))
                for key, value in before.items() if value['start'] and value['end']}

    def _validate_restore_dates(self, workorders, before):
        restored = self._restore_dates(before)
        if not restored:
            return
        start, end = map(datetime.fromisoformat, self.snapshot['inputs']['horizon'])
        self._validate_schedule_dates(workorders, restored, start, end, False)

    def _revert_reviewed_schedule(self, moved, workorders):
        before = self.application_snapshot['before']
        restored = self._restore_dates(before['workorders'])
        self._check_future_dates(moved, restored)
        self._validate_restore_dates(workorders, before['workorders'])
        self._write_schedule_dates(moved, restored)
        for production in moved.workorder_id.production_id:
            old = before['manufacturing'][str(production.id)]
            production.with_context(force_date=True).write({
                'date_start': datetime.fromisoformat(old['start']) if old['start'] else False,
                'date_finished': datetime.fromisoformat(old['end']) if old['end'] else False,
            })
        self.sudo().write({
            'state': 'reverted', 'reverted_at': fields.Datetime.now(), 'reverted_by': self.env.uid,
        })

    def _check_future_dates(self, moved, dates):
        now = fields.Datetime.now()
        for line in moved:
            order = line.workorder_id
            target = dates.get(order.id)
            if (order.state not in ('ready', 'blocked') or (order.date_start and order.date_start < now)
                    or (target and (not all(target) or target[0] < now))):
                raise UserError(_('An operation has started or a booking is in the past. No dates were updated.'))

    def _write_schedule_dates(self, moved, dates):
        for line in moved:
            order = line.workorder_id
            start, end = dates.get(order.id, (False, False))
            if not start:
                # Odoo clears the calendar booking before clearing dates on an unplanned operation.
                order.leave_id.unlink()
            order.with_context(bypass_duration_calculation=True).write({
                'date_start': start, 'date_finished': end,
            })
            booking = order.leave_id
            if ((order.date_start or False, order.date_finished or False) != (start, end)
                    or (booking.date_from or False, booking.date_to or False) != (start, end)):
                raise UserError(_('Odoo could not save the reviewed booking. No dates were updated.'))


class GridScheduleConfirm(models.TransientModel):
    _name = 'grid.schedule.confirm'
    _description = 'Confirm GridSync Schedule Change'

    run_id = fields.Many2one('grid.schedule.run', required=True, readonly=True)
    operation = fields.Selection([('apply', 'Apply'), ('revert', 'Revert')], required=True, readonly=True)

    def action_confirm(self):
        self.ensure_one()
        self.check_access('read')
        return self.run_id._confirm_schedule_change(self.operation)
