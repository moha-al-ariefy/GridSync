import base64
import hashlib
from datetime import timezone

from psycopg2.errors import UniqueViolation

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ConcurrencyError, UserError

from ..services.local_tariffs import MAX_CSV_BYTES
from ..services.meter_readings import MeterInputError, parse_meter_csv


class GridMeterBatch(models.Model):
    _name = 'grid.meter.batch'
    _description = 'GridSync Meter Gateway Batch'
    _check_company_auto = True
    _order = 'first_reading desc, id desc'

    name = fields.Char(required=True, readonly=True)
    company_id = fields.Many2one('res.company', required=True, index=True)
    workcenter_id = fields.Many2one('mrp.workcenter', required=True, check_company=True, ondelete='restrict')
    meter_reference = fields.Char(required=True)
    first_reading = fields.Datetime(required=True)
    last_reading = fields.Datetime(required=True)
    digest = fields.Char(required=True)
    readings_csv = fields.Binary(required=True, attachment=False)
    received_by = fields.Many2one('res.users', required=True, ondelete='restrict')

    _batch_identity = models.Constraint(
        'UNIQUE(company_id, workcenter_id, meter_reference, name)',
        'This meter gateway batch already exists.',
    )

    @api.depends('name', 'meter_reference')
    def _compute_display_name(self):
        for batch in self:
            batch.display_name = _('%(meter)s / %(batch)s', meter=batch.meter_reference, batch=batch.name)

    @api.model
    def submit_readings(self, workcenter_id, meter_reference, batch_reference, csv_text):
        if not self.env.user.has_group('mrp_grid_scheduler.group_grid_meter_gateway'):
            raise AccessError(_('Only a GridSync meter gateway can submit readings.'))
        if (type(workcenter_id) is not int or not 0 < workcenter_id <= 2_147_483_647
                or not isinstance(meter_reference, str)
                or not isinstance(batch_reference, str) or not isinstance(csv_text, str)):
            raise UserError(_('Provide a work center, meter reference, batch reference, and CSV text.'))
        meter_reference, batch_reference = meter_reference.strip(), batch_reference.strip()
        if not meter_reference or not batch_reference or max(len(meter_reference), len(batch_reference)) > 120:
            raise UserError(_('Use meter and batch references between 1 and 120 characters.'))
        if len(csv_text) > MAX_CSV_BYTES:
            raise UserError(_('Provide meter readings up to 2 MB.'))
        center = self.env['mrp.workcenter'].sudo().search([
            ('id', '=', workcenter_id), ('company_id', '=', self.env.company.id),
        ], limit=1)
        if not center or center not in self.env.user.sudo().grid_meter_workcenter_ids:
            raise AccessError(_('This gateway is not assigned to that work center.'))
        try:
            content = csv_text.encode('utf-8')
            readings = parse_meter_csv(content)
        except (MeterInputError, UnicodeError) as exc:
            raise UserError(str(exc)) from exc
        canonical = 'start,end,kwh\n' + ''.join(
            f'{start.replace(tzinfo=timezone.utc).isoformat()},{end.replace(tzinfo=timezone.utc).isoformat()},{(kwh.normalize() or 0):f}\n'
            for start, end, kwh in readings
        )
        identity = {
            'company_id': self.env.company.id, 'workcenter_id': center.id,
            'meter_reference': meter_reference, 'name': batch_reference,
        }
        digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        batches = self.sudo()
        batch = batches.search([(field, '=', value) for field, value in identity.items()], limit=1)
        if batch:
            if batch.digest != digest:
                raise UserError(_('This batch reference was already used for different readings.'))
            return batch.id
        try:
            batch = batches.create({
                **identity, 'digest': digest, 'first_reading': readings[0][0],
                'last_reading': readings[-1][1], 'readings_csv': base64.b64encode(content),
                'received_by': self.env.uid,
            })
        except UniqueViolation as exc:
            if exc.diag.constraint_name != 'grid_meter_batch_batch_identity':
                raise
            raise ConcurrencyError('Another request saved this meter batch.') from exc
        return batch.id


class ResUsers(models.Model):
    _inherit = 'res.users'

    grid_meter_workcenter_ids = fields.Many2many(
        'mrp.workcenter', string='GridSync gateway work centers', groups='base.group_system',
    )
