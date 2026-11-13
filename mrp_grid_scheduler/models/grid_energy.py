import base64
import binascii
import math
from datetime import datetime, timedelta
from decimal import Decimal

from psycopg2.errors import UniqueViolation

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ConcurrencyError, UserError

from ..services.local_tariffs import MAX_CSV_BYTES
from ..services.meter_readings import MeterInputError, merge_times, parse_meter_csv, price_readings
from ..services.scheduler import MAX_HORIZON, floor_slot
from .mrp_workcenter import GRID_MANAGER, GRID_PLANNER


class GridEnergyResult(models.Model):
    _name = 'grid.energy.result'
    _description = 'GridSync Meter Result'
    _check_company_auto = True

    line_id = fields.Many2one('grid.schedule.line', required=True, ondelete='restrict', readonly=True, index=True)
    company_id = fields.Many2one(related='line_id.company_id', store=True, index=True)
    workorder_id = fields.Many2one('mrp.workorder', required=True, ondelete='restrict', readonly=True, check_company=True)
    tariff_id = fields.Many2one('grid.tariff.source', required=True, ondelete='restrict', readonly=True, check_company=True)
    currency_id = fields.Many2one('res.currency', required=True, readonly=True)
    first_reading = fields.Datetime(readonly=True)
    last_reading = fields.Datetime(readonly=True)
    meter_reference = fields.Char(required=True, readonly=True)
    measured_kwh = fields.Float('Metered energy (kWh)', digits=(20, 6), readonly=True)
    energy_charge = fields.Monetary('Calculated energy charge', readonly=True)
    baseline_charge = fields.Monetary('Estimated original charge', readonly=True)
    estimated_savings = fields.Monetary('Estimated energy-charge savings', readonly=True)
    has_baseline = fields.Boolean(readonly=True)
    baseline_note = fields.Char(readonly=True)
    has_bill = fields.Boolean(readonly=True)
    billed_charge = fields.Monetary('Billed energy charge', readonly=True)
    bill_difference = fields.Monetary('Billed minus calculated', readonly=True)
    bill_reference = fields.Char(readonly=True)
    bill_line_id = fields.Many2one(
        'account.move.line', readonly=True, ondelete='set null', check_company=True,
        groups='account.group_account_invoice',
    )
    bill_checked_by = fields.Many2one('res.users', readonly=True)
    bill_checked_at = fields.Datetime(readonly=True)
    can_compare_bill = fields.Boolean(compute="_compute_can_compare_bill")
    imported_by = fields.Many2one('res.users', readonly=True)
    evidence = fields.Json(readonly=True)
    has_emissions_estimate = fields.Boolean(readonly=True)
    estimated_emissions_kg = fields.Float('Estimated electricity emissions (kg CO₂e)', digits=(20, 6), readonly=True)
    emissions_reference = fields.Char('Electricity factor source', readonly=True)
    gateway_batch_reference = fields.Char(compute='_compute_gateway_batch_reference', string='Gateway batch')

    _one_result_per_operation = models.Constraint('UNIQUE(workorder_id)', 'This operation already has a meter result.')

    @api.depends('evidence')
    def _compute_gateway_batch_reference(self):
        for result in self:
            evidence = result.evidence or {}
            batch_id = evidence.get('gateway_batch_id')
            reference = evidence.get('gateway_batch_reference')
            result.gateway_batch_reference = reference or (_('Batch #%s', batch_id) if batch_id else False)

    @api.depends('has_bill', 'evidence')
    def _compute_can_compare_bill(self):
        for result in self:
            result.can_compare_bill = not result.has_bill and len((result.evidence or {}).get('run_times', [])) == 1

    def action_open_bill_check(self):
        self.ensure_one()
        self._check_bill_access()
        return {
            'type': 'ir.actions.act_window', 'name': _('Compare energy bill'),
            'res_model': 'grid.energy.bill.check', 'view_mode': 'form', 'target': 'new',
            'context': {'default_result_id': self.id},
        }

    def _check_bill_access(self):
        self.ensure_one()
        self.check_access('read')
        if not self.env.user.has_group(GRID_MANAGER):
            raise AccessError(_('Only a GridSync manager can compare a bill.'))
        if self.has_bill:
            raise UserError(_('This result already has a saved bill comparison.'))
        if len((self.evidence or {}).get('run_times', [])) != 1:
            raise UserError(_('A bill comparison needs one continuous period of meter readings.'))

    def _open_result(self):
        self.ensure_one()
        self.check_access('read')
        return {
            'type': 'ir.actions.act_window', 'name': _('Energy result'), 'res_model': self._name,
            'res_id': self.id, 'view_mode': 'form', 'target': 'new',
        }


class GridScheduleLine(models.Model):
    _inherit = 'grid.schedule.line'

    tariff_source_id = fields.Many2one('grid.tariff.source', readonly=True, ondelete='set null')
    energy_result_ids = fields.One2many('grid.energy.result', 'line_id', readonly=True)
    operation_state = fields.Selection(related='workorder_id.state')

    def _meter_tariffs(self):
        self.ensure_one()
        ids = self.tariff_source_id.ids
        if not ids:
            sources = (self.run_id.snapshot or {}).get('sources', [])
            ids = [sources[0]['id']] if len(sources) == 1 else []
        return self.env['grid.tariff.source'].with_context(active_test=False).search([
            ('id', 'in', ids), ('company_id', '=', self.company_id.id),
            ('source_type', '!=', 'energy_charts'),
            '|', ('source_type', '!=', 'csv'), ('csv_price_basis', '!=', 'forecast'),
        ])

    def _check_meter_import(self):
        self.ensure_one()
        self.check_access('read')
        if not self.env.user.has_group(GRID_MANAGER):
            raise AccessError(_('Only a GridSync manager can import meter readings.'))
        order = self.workorder_id
        order.check_access('read')
        if (self.run_id.state != 'applied' or self.status != 'moved' or order.state != 'done'
                or order.workcenter_id != self.workcenter_id or order.company_id != self.company_id):
            raise UserError(_('Choose a completed operation from an applied schedule on the same work center.'))
        if self.search_count([
            ('workorder_id', '=', order.id), ('status', '=', 'moved'), ('run_id.state', '=', 'applied'),
            '|', ('run_id.applied_at', '>', self.run_id.applied_at),
            '&', ('run_id.applied_at', '=', self.run_id.applied_at), ('run_id', '>', self.run_id.id),
        ], limit=1):
            raise UserError(_('Import readings from the last schedule applied to this operation.'))

    def action_open_meter_import(self):
        self._check_meter_import()
        tariffs = self._meter_tariffs()
        if not tariffs:
            raise UserError(_('The proposal needs an available site tariff with rates for the energy charge.'))
        return {
            'type': 'ir.actions.act_window', 'name': _('Import meter readings'),
            'res_model': 'grid.energy.import', 'view_mode': 'form', 'target': 'new',
            'context': {'default_line_id': self.id, 'default_tariff_id': tariffs.id if len(tariffs) == 1 else False},
        }

    def action_open_energy_result(self):
        self.ensure_one()
        self.check_access('read')
        return self.energy_result_ids._open_result()

    def _meter_work_times(self, readings):
        order = self.workorder_id
        timers = order.time_ids.filtered(lambda timer: timer.loss_type in ('productive', 'performance'))
        if not timers or any(not timer.date_end or timer.date_end <= timer.date_start
                             or timer.workcenter_id != self.workcenter_id for timer in timers):
            raise UserError(_('The operation needs completed run times on the reviewed work center.'))
        times = merge_times((timer.date_start, timer.date_end) for timer in timers)
        start, end = times[0][0], times[-1][1]
        if start < self.run_id.applied_at or end > fields.Datetime.now() or end - start > MAX_HORIZON:
            raise UserError(_('Use completed run times after Apply, spanning at most 14 days.'))
        if merge_times((low, high) for low, high, _ in readings) != times:
            raise UserError(_('Readings must cover the logged run times exactly. Check for gaps and readings outside the operation.'))
        others = self.env['mrp.workcenter.productivity'].search([
            ('company_id', '=', self.company_id.id), ('workcenter_id', '=', self.workcenter_id.id),
            ('workorder_id', '!=', order.id), ('workorder_id', '!=', False),
            ('loss_type', 'in', ['productive', 'performance']), ('date_start', '<', end),
            '|', ('date_end', '=', False), ('date_end', '>', start),
        ])
        if any(timer.date_start < high and (not timer.date_end or timer.date_end > low)
               for timer in others for low, high in times):
            raise UserError(_('Another operation ran on this work center at the same time. Its meter usage cannot be assigned here.'))
        return times

    def _meter_prices(self, tariff, start, end):
        slots = self.env['grid.tariff.slot'].search([
            ('source_id', '=', tariff.id), ('is_current', '=', True),
            ('start_utc', '>=', floor_slot(start)), ('start_utc', '<', end),
        ])
        return {slot.start_utc: Decimal(str(slot.price_per_mwh)) for slot in slots}

    def _meter_baseline(self, tariff, readings, times):
        if not self.current_start or not self.current_end:
            return None, {}, _('The operation had no original booking to compare.')
        offset = self.current_start - times[0][0]
        original_end = times[-1][1] + offset
        center = next((center for center in self.run_id.snapshot['inputs']['workcenters']
                       if center['key'] == self.workcenter_id.id), None)
        working = merge_times(tuple(map(datetime.fromisoformat, pair)) for pair in center['working']) if center else []
        if original_end > self.current_end or any(
            not any(low <= start + offset and end + offset <= high for low, high in working)
            for start, end in times
        ):
            return None, {}, _('The measured run does not fit the original working hours and booking.')
        prices = self._meter_prices(tariff, self.current_start, original_end)
        try:
            charge, used = price_readings(readings, prices, offset)
        except MeterInputError as exc:
            return None, {}, str(exc)
        return charge, {key: prices[key] for key in used}, False


class MrpWorkorder(models.Model):
    _inherit = 'mrp.workorder'

    grid_energy_result_ids = fields.One2many('grid.energy.result', 'workorder_id', groups=GRID_PLANNER)

    def action_open_grid_energy_result(self):
        self.ensure_one()
        self.check_access('read')
        return self.grid_energy_result_ids._open_result()


class GridEnergyImport(models.TransientModel):
    _name = 'grid.energy.import'
    _description = 'Import GridSync Meter Readings'

    line_id = fields.Many2one('grid.schedule.line', required=True, readonly=True)
    tariff_ids = fields.Many2many('grid.tariff.source', compute='_compute_tariff_ids')
    tariff_id = fields.Many2one('grid.tariff.source', required=True)
    meter_reference = fields.Char()
    dedicated_meter = fields.Boolean('Meter covers only this operation during the uploaded intervals')
    reading_source = fields.Selection([
        ('csv', 'Meter CSV'), ('gateway', 'Meter gateway'),
    ], required=True, default='csv')
    gateway_batch_id = fields.Many2one('grid.meter.batch')
    workcenter_id = fields.Many2one(related='line_id.workcenter_id')
    company_id = fields.Many2one(related='line_id.company_id')
    csv_file = fields.Binary(attachment=False)
    csv_filename = fields.Char()
    compare_bill = fields.Boolean('Compare with a billed energy charge')
    currency_id = fields.Many2one(related='tariff_id.currency_id')
    billed_charge = fields.Monetary()
    bill_reference = fields.Char()
    bill_start = fields.Datetime()
    bill_end = fields.Datetime()

    @api.depends('line_id')
    def _compute_tariff_ids(self):
        for wizard in self:
            wizard.tariff_ids = wizard.line_id._meter_tariffs() if wizard.line_id else False

    def action_import(self):
        self.ensure_one()
        self.check_access('read')
        line = self.line_id
        line._check_meter_import()
        tariff = self.tariff_id
        tariff.check_access('read')
        if tariff not in line._meter_tariffs():
            raise UserError(_('Choose a site tariff from this proposal with rates for the energy charge.'))
        meter_reference = self.meter_reference
        batch = self.env['grid.meter.batch']
        if self.reading_source == 'gateway':
            batch = self.gateway_batch_id
            batch.check_access('read')
            if not batch or batch.company_id != line.company_id or batch.workcenter_id != line.workcenter_id:
                raise UserError(_('Select a meter batch for this work center and company.'))
            readings = self._parse_meter_file(batch.readings_csv)
            meter_reference = batch.meter_reference
        else:
            readings = self._parse_meter_file(self.csv_file)
        if not self.dedicated_meter or not (meter_reference or '').strip():
            raise UserError(_('Enter a meter reference and confirm the readings cover only this operation.'))
        times = line._meter_work_times(readings)
        start, end = times[0][0], times[-1][1]
        bill_reference = (self.bill_reference or '').strip() if self.compare_bill else ''
        if self.compare_bill and (
            len(times) != 1 or (self.bill_start, self.bill_end) != (start, end)
            or not bill_reference or not math.isfinite(self.billed_charge)
        ):
            raise UserError(_('Enter a bill reference and a billing period that matches the continuous meter coverage.'))
        currency = tariff.currency_id
        billed = currency.round(self.billed_charge) if self.compare_bill else 0
        meter_reference = meter_reference.strip()
        saved_readings = [[low.isoformat(), high.isoformat(), str(kwh.normalize() or 0)] for low, high, kwh in readings]
        results = self.env['grid.energy.result']
        existing = results.search([('workorder_id', '=', line.workorder_id.id)], limit=1)
        if existing:
            same_readings = (existing.line_id, existing.tariff_id, existing.evidence['readings'],
                             existing.meter_reference) == (line, tariff, saved_readings, meter_reference)
            same_bill = not self.compare_bill or (
                existing.has_bill and existing.billed_charge == billed and existing.bill_reference == bill_reference
            )
            if same_readings and same_bill:
                return existing._open_result()
            raise UserError(_('This operation already has a saved meter result.'))

        prices = line._meter_prices(tariff, start, end)
        try:
            actual, used = price_readings(readings, prices)
        except MeterInputError as exc:
            raise UserError(str(exc)) from exc
        baseline, baseline_prices, note = line._meter_baseline(tariff, readings, times)
        actual = currency.round(float(actual))
        original = currency.round(float(baseline)) if baseline is not None else 0
        evidence = {
            'readings': saved_readings,
            'run_times': [[low.isoformat(), high.isoformat()] for low, high in times],
            'actual_prices': {slot.isoformat(): str(prices[slot]) for slot in sorted(used)},
            'baseline_prices': {slot.isoformat(): str(price) for slot, price in sorted(baseline_prices.items())},
            'tariff_reference': tariff.source_reference,
            'gateway_batch_id': batch.id,
            'gateway_batch_reference': batch.name,
        }
        measured_kwh = sum(kwh for _, _, kwh in readings)
        center = line.workcenter_id
        emissions = {}
        if center.grid_use_carbon_factor:
            factor = Decimal(str(center.grid_carbon_factor))
            evidence['emissions_factor_g_co2e_per_kwh'] = str(factor)
            emissions = {
                'has_emissions_estimate': True,
                'estimated_emissions_kg': float(measured_kwh * factor / 1000),
                'emissions_reference': center.grid_carbon_reference,
            }
        values = {
            'line_id': line.id, 'workorder_id': line.workorder_id.id, 'tariff_id': tariff.id, 'currency_id': currency.id,
            'first_reading': start, 'last_reading': end, 'meter_reference': meter_reference,
            'measured_kwh': float(measured_kwh), 'energy_charge': actual,
            'baseline_charge': original, 'has_baseline': baseline is not None, 'baseline_note': note,
            'estimated_savings': currency.round(original - actual) if baseline is not None else 0,
            'has_bill': self.compare_bill, 'billed_charge': billed,
            'bill_checked_by': self.env.uid if self.compare_bill else False,
            'bill_checked_at': fields.Datetime.now() if self.compare_bill else False,
            'bill_reference': bill_reference or False,
            'bill_difference': currency.round(billed - actual) if self.compare_bill else 0,
            'imported_by': self.env.uid, 'evidence': evidence, **emissions,
        }
        try:
            result = results.sudo().create(values)
        except UniqueViolation as exc:
            if exc.diag.constraint_name != 'grid_energy_result_one_result_per_operation':
                raise
            # Odoo retries with a fresh snapshot if another import got there first.
            raise ConcurrencyError('Another import saved the meter result.') from exc
        return result.with_env(self.env)._open_result()


    @api.model
    def _parse_meter_file(self, content):
        if not content or len(content) > ((MAX_CSV_BYTES + 2) // 3) * 4:
            raise UserError(_('Provide meter readings up to 2 MB.'))
        try:
            return parse_meter_csv(base64.b64decode(content, validate=True))
        except binascii.Error as exc:
            raise UserError(_('Provide a valid meter CSV.')) from exc
        except MeterInputError as exc:
            raise UserError(str(exc)) from exc


class GridEnergyBillCheck(models.TransientModel):
    _name = 'grid.energy.bill.check'
    _description = 'Compare GridSync Energy Bill'

    result_id = fields.Many2one('grid.energy.result', required=True, readonly=True, ondelete='cascade')
    company_id = fields.Many2one(related='result_id.company_id', compute_sudo=False)
    currency_id = fields.Many2one(related='result_id.currency_id', compute_sudo=False)
    first_reading = fields.Datetime(related='result_id.first_reading', compute_sudo=False)
    last_reading = fields.Datetime(related='result_id.last_reading', compute_sudo=False)
    bill_line_id = fields.Many2one('account.move.line', groups='account.group_account_invoice')
    vendor_id = fields.Many2one(
        related='bill_line_id.partner_id', compute_sudo=False, groups='account.group_account_invoice',
    )
    invoice_date = fields.Date(
        related='bill_line_id.move_id.invoice_date', compute_sudo=False, groups='account.group_account_invoice',
    )
    vendor_line_subtotal = fields.Monetary(
        'Untaxed line subtotal', related='bill_line_id.price_subtotal',
        compute_sudo=False, groups='account.group_account_invoice',
    )
    bill_reference = fields.Char()
    use_vendor_bill = fields.Boolean('Use a posted vendor bill line', groups='account.group_account_invoice')
    bill_start = fields.Datetime(required=True)
    bill_end = fields.Datetime(required=True)
    billed_charge = fields.Monetary()

    def action_compare(self):
        self.ensure_one()
        self.check_access('read')
        result = self.result_id
        result._check_bill_access()
        reference = (self.bill_reference or '').strip()
        amount = self.billed_charge
        bill_line = self.env['account.move.line']
        if self.env.user.has_group('account.group_account_invoice') and self.use_vendor_bill:
            bill_line = self.bill_line_id
            bill_line.check_access('read')
            if (not bill_line or bill_line.move_id.move_type != 'in_invoice'
                    or bill_line.move_id.state != 'posted' or bill_line.display_type != 'product'
                    or bill_line.company_id != result.company_id or bill_line.currency_id != result.currency_id):
                raise UserError(_('Choose a posted vendor bill charge in the same company and currency.'))
            reference = bill_line.move_id.name
            amount = bill_line.price_subtotal
        if (not reference or not math.isfinite(amount)
                or (self.bill_start, self.bill_end) != (result.first_reading, result.last_reading)):
            raise UserError(_('Enter a bill reference and an energy charge for the exact metered period.'))

        self.env.cr.execute(
            'SELECT id FROM grid_energy_result WHERE id = %s FOR NO KEY UPDATE', [result.id],
        )
        result.invalidate_recordset(['has_bill'])
        result._check_bill_access()
        currency = result.currency_id
        billed = currency.round(amount)
        result.sudo().write({
            'bill_line_id': bill_line.id,
            'has_bill': True, 'billed_charge': billed, 'bill_reference': reference,
            'bill_difference': currency.round(billed - result.energy_charge),
            'bill_checked_by': self.env.uid, 'bill_checked_at': fields.Datetime.now(),
        })
        return result._open_result()
