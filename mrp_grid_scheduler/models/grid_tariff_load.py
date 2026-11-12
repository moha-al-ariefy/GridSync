import base64
import binascii
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services.local_tariffs import MAX_CSV_BYTES, TariffInputError, parse_tariff_csv


class GridTariffLoad(models.TransientModel):
    _name = 'grid.tariff.load'
    _description = 'Load GridSync Price Intervals'

    source_id = fields.Many2one('grid.tariff.source', required=True, readonly=True)
    source_type = fields.Selection(related='source_id.source_type')
    first_day = fields.Date()
    last_day = fields.Date()
    csv_file = fields.Binary(attachment=False)
    csv_filename = fields.Char()
    replace_existing = fields.Boolean()

    @api.model
    def default_get(self, fields_list):
        result = super().default_get(fields_list)
        source = self.env['grid.tariff.source'].browse(result.get('source_id'))
        if not source:
            return result
        source._check_local_manager()
        if source.source_type == 'manual_tou':
            today = fields.Datetime.context_timestamp(
                source.with_context(tz=source.timezone), fields.Datetime.now(),
            ).date()
            first_day = today if source.valid_from <= today <= source.valid_to else source.valid_from
            result.update(first_day=first_day, last_day=min(first_day + timedelta(days=6), source.valid_to))
        return result

    def action_load(self):
        self.ensure_one()
        source = self.source_id
        source._check_local_manager()
        if source.source_type == 'manual_tou':
            return source._generate_profile_intervals(self.first_day, self.last_day)
        if not self.csv_file or len(self.csv_file) > ((MAX_CSV_BYTES + 2) // 3) * 4:
            raise UserError(_('Upload a CSV file up to 2 MB.'))
        try:
            content = base64.b64decode(self.csv_file, validate=True)
            rows, missing = parse_tariff_csv(content, source.currency_id.name)
        except binascii.Error as exc:
            raise UserError(_('Upload a valid CSV file.')) from exc
        except TariffInputError as exc:
            raise UserError(str(exc)) from exc
        return source._save_local_intervals(rows, missing, self.replace_existing)
