import json

import requests

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..services.carbon_intensity import parse_neso_intensity
from ..services.energy_charts import PriceDataError


NESO_URL = 'https://api.carbonintensity.org.uk/intensity/date'


class GridCarbonDay(models.Model):
    _name = 'grid.carbon.day'
    _description = 'Great Britain grid carbon intensity'
    _rec_name = 'day'
    _order = 'day desc, id desc'

    day = fields.Date(required=True, default=lambda self: fields.Datetime.now().date(), string='UTC date')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    synced_at = fields.Datetime(readonly=True, copy=False)
    interval_ids = fields.One2many('grid.carbon.interval', 'day_id', readonly=True, copy=False)

    _unique_day = models.Constraint('UNIQUE(company_id, day)', 'This UTC date already has a carbon source.')

    def write(self, values):
        if not self.env.su and {'day', 'company_id', 'synced_at', 'interval_ids'}.intersection(values):
            raise AccessError(_('Create a new carbon day to select another date.'))
        return super().write(values)

    @api.model_create_multi
    def create(self, values_list):
        if not self.env.su:
            values_list = [dict(values, synced_at=False, interval_ids=[]) for values in values_list]
        return super().create(values_list)

    def action_sync(self):
        self.ensure_one()
        self.check_access('write')
        if not self.env.user.has_group('mrp_grid_scheduler.group_grid_manager'):
            raise AccessError(_('Only a GridSync manager can sync carbon intensity.'))
        if not self.try_lock_for_update():
            raise UserError(_('This carbon day is already being refreshed.'))
        try:
            with requests.get(
                f'{NESO_URL}/{self.day.isoformat()}', timeout=(4, 12), stream=True, allow_redirects=False,
                headers={'User-Agent': 'GridSync-MRP'},
            ) as response:
                if response.status_code != 200:
                    raise PriceDataError('NESO returned an unsuccessful response.')
                body = bytearray()
                for chunk in response.iter_content(8192):
                    body.extend(chunk)
                    if len(body) > 65_536:
                        raise PriceDataError('The NESO response is too large.')
                intervals = parse_neso_intensity(json.loads(body), self.day)
        except (ValueError, requests.RequestException) as exc:
            raise UserError(_('Could not refresh NESO carbon intensity. Saved intervals are still available.')) from exc

        existing = {interval.start_utc: interval for interval in self.interval_ids.sudo()}
        new = []
        for start, readings in intervals.items():
            values = {}
            for basis, value in readings.items():
                if value is not None:
                    values.update({basis: value, f'has_{basis}': True})
            if start in existing:
                existing[start].write(values)
            else:
                new.append({'day_id': self.id, 'start_utc': start, **values})
        self.env['grid.carbon.interval'].sudo().create(new)
        self.sudo().write({'synced_at': fields.Datetime.now()})
        return True


class GridCarbonInterval(models.Model):
    _name = 'grid.carbon.interval'
    _description = 'Grid carbon intensity half-hour'
    _order = 'start_utc'

    day_id = fields.Many2one('grid.carbon.day', required=True, index=True, ondelete='cascade')
    company_id = fields.Many2one(related='day_id.company_id', store=True, index=True)
    start_utc = fields.Datetime(required=True)
    forecast = fields.Float('Forecast gCO₂/kWh')
    actual = fields.Float('Published estimate gCO₂/kWh')
    has_forecast = fields.Boolean()
    has_actual = fields.Boolean()

    _unique_start = models.Constraint('UNIQUE(day_id, start_utc)', 'This half-hour is already saved.')
