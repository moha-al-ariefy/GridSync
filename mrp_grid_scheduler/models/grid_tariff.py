import json
import math
from datetime import timedelta
import pytz
import requests

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from ..services.energy_charts import PriceDataError, parse_prices


ENERGY_CHARTS_URL = 'https://api.energy-charts.info/v2/price'
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
AUTO_SYNC_INTERVAL = timedelta(minutes=5)


class GridTariffSource(models.Model):
    _name = 'grid.tariff.source'
    _description = 'GridSync Tariff Source'
    _check_company_auto = True
    _order = 'name, id'

    name = fields.Char(required=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company, index=True)
    source_type = fields.Selection([
        ('manual', 'Manual'), ('energy_charts', 'Energy-Charts DE-LU'),
    ], required=True, default='manual')
    currency_id = fields.Many2one('res.currency', required=True, default=lambda self: self.env.company.currency_id)
    timezone = fields.Char(required=True, default='Asia/Dubai')
    source_reference = fields.Char(help='The tariff document or data source reference.')
    attribution = fields.Char(help='The attribution required by the source.')
    slot_ids = fields.One2many('grid.tariff.slot', 'source_id', string='Price Intervals')
    sync_ids = fields.One2many('grid.tariff.sync', 'source_id', string='Sync History')
    last_sync_at = fields.Datetime(readonly=True, copy=False)
    generated_at = fields.Datetime(readonly=True, copy=False)
    license_text = fields.Char(readonly=True, copy=False)
    active = fields.Boolean(default=True)

    @api.onchange('source_type')
    def _onchange_source_type(self):
        if self.source_type == 'energy_charts':
            self.timezone = 'Europe/Berlin'
            self.currency_id = self.env.ref('base.EUR')
            self.source_reference = ENERGY_CHARTS_URL
            self.attribution = 'Energy-Charts.info, Bundesnetzagentur | SMARD.de, CC BY 4.0'

    @api.constrains('timezone', 'source_type', 'currency_id', 'source_reference')
    def _check_source_settings(self):
        for source in self:
            try:
                pytz.timezone(source.timezone)
            except (pytz.UnknownTimeZoneError, ValueError, TypeError) as exc:
                raise ValidationError(_('Use a valid timezone, such as Asia/Dubai.')) from exc
            if source.source_type == 'energy_charts' and (
                source.timezone != 'Europe/Berlin' or source.currency_id.name != 'EUR'
            ):
                raise ValidationError(_('Energy-Charts DE-LU prices need the Europe/Berlin timezone and EUR.'))
            if not source.source_reference or not source.source_reference.strip():
                raise ValidationError(_('Add a source reference for this tariff.'))

    @api.model_create_multi
    def create(self, values_list):
        default_type = self.default_get(['source_type']).get('source_type', 'manual')
        for values in values_list:
            if not self.env.su and {'last_sync_at', 'generated_at', 'license_text'}.intersection(values):
                raise AccessError(_('Sync metadata can only be changed by a sync.'))
            if not self.env.su:
                values.update({'last_sync_at': False, 'generated_at': False, 'license_text': False})
            values.setdefault('source_type', default_type)
            if values['source_type'] == 'energy_charts':
                values['source_reference'] = ENERGY_CHARTS_URL
                values['attribution'] = 'Energy-Charts.info, Bundesnetzagentur | SMARD.de, CC BY 4.0'
        return super().create(values_list)

    def write(self, values):
        if not self.env.su and values.get('company_id') and values['company_id'] not in self.env.companies.ids:
            raise AccessError(_('The selected company is not available to this user.'))
        if 'source_type' in values and any(source.source_type != values['source_type'] for source in self):
            raise UserError(_('Create a new tariff source instead of changing its type.'))
        if not self.env.su and {'last_sync_at', 'generated_at', 'license_text'}.intersection(values):
            raise AccessError(_('Sync metadata can only be changed by a sync.'))
        if not self.env.su and {'source_reference', 'attribution'}.intersection(values) and self.filtered(lambda s: s.source_type == 'energy_charts'):
            raise AccessError(_('Energy-Charts source details are set by the provider.'))
        protected = {'source_type', 'company_id', 'currency_id', 'timezone'}
        if protected.intersection(values) and any(self.mapped('slot_ids')):
            raise UserError(_('Create a new tariff source instead of changing settings for existing intervals.'))
        return super().write(values)

    def action_sync_energy_charts(self):
        self.ensure_one()
        self.check_access('write')
        if not self.env.user.has_group('mrp_grid_scheduler.group_grid_manager'):
            raise AccessError(_('Only a GridSync manager can sync prices.'))
        if self.source_type != 'energy_charts':
            raise UserError(_('This source is not an Energy-Charts source.'))
        message, level = self._sync_energy_charts()
        if level == 'busy':
            raise UserError(message)
        return self._sync_notice(message, level)

    @api.model
    def refresh_energy_charts(self, source_id=None):
        if not self.env.user.has_group('mrp_grid_scheduler.group_grid_planner'):
            raise AccessError(_('Only GridSync users can refresh prices.'))
        domain = [('active', '=', True), ('source_type', '=', 'energy_charts')]
        if source_id is not None:
            if type(source_id) is not int:
                raise UserError(_('Select a valid tariff source.'))
            domain.append(('id', '=', source_id))
        sources = self.search(domain)
        return sources.sudo()._refresh_due_energy_charts()

    @api.model
    def _cron_sync_energy_charts(self):
        self.sudo().search([
            ('active', '=', True), ('source_type', '=', 'energy_charts'),
            ('company_id.grid_auto_sync', '=', True),
        ])._refresh_due_energy_charts()

    def _refresh_due_energy_charts(self):
        if not self:
            return False
        _, level = self._sync_energy_charts(automatic=True)
        return level == 'success'

    def _sync_energy_charts(self, automatic=False):
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(%s)', [510884413214])
        if not self.env.cr.fetchone()[0]:
            return _('Another price sync is running. Try again shortly.'), 'busy'
        now = fields.Datetime.now()
        if automatic:
            self = self.filtered_domain([
                ('active', '=', True), ('source_type', '=', 'energy_charts'),
                ('company_id.grid_auto_sync', '=', True),
                ('sync_ids', 'not any', [('started_at', '>', now - AUTO_SYNC_INTERVAL)]),
            ])
            if not self:
                return '', 'skipped'
        # The API limit applies across companies.
        logs = self.env['grid.tariff.sync'].sudo()
        previous = logs.search([('provider_type', '=', 'energy_charts')], order='started_at desc, id desc', limit=1)
        if previous and previous.next_allowed_at and previous.next_allowed_at > now:
            return _('Please wait before requesting prices again.'), 'busy'
        logs = logs.create([{
            'source_id': source.id, 'provider_type': 'energy_charts', 'started_at': now,
            'next_allowed_at': now + timedelta(seconds=30), 'state': 'failed',
        } for source in self])
        try:
            with requests.get(
                ENERGY_CHARTS_URL, params={'bzn': 'DE-LU'}, stream=True,
                allow_redirects=False, timeout=(4, 12),
                headers={'User-Agent': 'GridSync-MRP/0.1'},
            ) as response:
                if response.status_code == 429:
                    retry = response.headers.get('Retry-After', '')
                    seconds = min(max(int(retry), 30), 3600) if retry.isdigit() else 60
                    logs.write({'next_allowed_at': now + timedelta(seconds=seconds), 'message': 'Rate limit reached.'})
                    return _('The data provider asked GridSync to wait before trying again.'), 'warning'
                if response.status_code != 200:
                    raise PriceDataError('The data provider returned an unsuccessful response.')
                body = bytearray()
                for chunk in response.iter_content(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise PriceDataError('The data response is too large.')
                parsed = parse_prices(json.loads(body))
        except (requests.RequestException, ValueError, UnicodeError, PriceDataError):
            logs.write({'message': 'The price data could not be fetched or validated.'})
            return _('Price sync failed. The previous data is still available.'), 'danger'
        for log in logs:
            log.source_id._save_energy_charts_prices(parsed, log)
        return _('Price sync completed.'), 'success'

    def _save_energy_charts_prices(self, parsed, log):
        now = log.started_at
        slots = self.env['grid.tariff.slot'].sudo()
        existing = slots.search([
            ('source_id', '=', self.id), ('start_utc', 'in', [start for start, _ in parsed['slots']]),
        ])
        existing_by_start = {slot.start_utc: slot for slot in existing}
        new_slots = []
        for start, price in parsed['slots']:
            values = {'price_per_mwh': price, 'is_current': True, 'observed_at': now}
            if start in existing_by_start:
                existing_by_start[start].write(values)
            else:
                new_slots.append({'source_id': self.id, 'start_utc': start, **values})
        slots.create(new_slots)
        if parsed['missing_starts']:
            slots.search([
                ('source_id', '=', self.id), ('start_utc', 'in', parsed['missing_starts']),
            ]).write({'is_current': False})
        self.sudo().write({
            'last_sync_at': now, 'generated_at': parsed['generated_at'],
            'license_text': parsed['license'],
            'attribution': 'Energy-Charts.info, Bundesnetzagentur | SMARD.de, CC BY 4.0',
        })
        log.write({
            'state': 'success', 'inserted': len(new_slots),
            'updated': len(existing), 'missing': parsed['missing'],
        })

    def _sync_notice(self, message, level):
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('GridSync'), 'message': message, 'type': level, 'sticky': level != 'success',
            'next': {'type': 'ir.actions.client', 'tag': 'reload'},
        }}


class GridTariffSlot(models.Model):
    _name = 'grid.tariff.slot'
    _description = 'GridSync Price Interval'
    _check_company_auto = True
    _order = 'start_utc, id'

    source_id = fields.Many2one('grid.tariff.source', required=True, ondelete='cascade', index=True, check_company=True)
    company_id = fields.Many2one(related='source_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='source_id.currency_id', store=True)
    start_utc = fields.Datetime(required=True, index=True)
    price_per_mwh = fields.Float(string='Price per MWh', required=True, digits=(16, 6))
    minutes = fields.Integer(default=15, required=True, readonly=True)
    is_current = fields.Boolean(default=True, readonly=True)
    observed_at = fields.Datetime(readonly=True, copy=False)

    _source_start_unique = models.Constraint('UNIQUE(source_id, start_utc)', 'This source already has an interval at this time.')
    _duration_fixed = models.Constraint('CHECK(minutes = 15)', 'Price intervals must be 15 minutes.')

    @api.constrains('start_utc', 'price_per_mwh')
    def _check_interval(self):
        for slot in self:
            if slot.start_utc and (slot.start_utc.minute % 15 or slot.start_utc.second or slot.start_utc.microsecond):
                raise ValidationError(_('The interval must start on a 15-minute boundary.'))
            if not math.isfinite(slot.price_per_mwh):
                raise ValidationError(_('The price must be a finite number.'))

    @api.model_create_multi
    def create(self, values_list):
        if not self.env.su:
            if any({'is_current', 'observed_at'}.intersection(values) for values in values_list):
                raise AccessError(_('Interval status can only be changed by a sync.'))
            default_source = self.default_get(['source_id']).get('source_id')
            for values in values_list:
                values.setdefault('source_id', default_source)
                values.update({'is_current': True, 'observed_at': False})
            sources = self.env['grid.tariff.source'].browse([v.get('source_id') for v in values_list if v.get('source_id')])
            if any(source.source_type == 'energy_charts' for source in sources):
                raise AccessError(_('Energy-Charts intervals can only be changed by syncing the source.'))
        return super().create(values_list)

    def write(self, values):
        if not self.env.su and {'is_current', 'observed_at'}.intersection(values):
            raise AccessError(_('Interval status can only be changed by a sync.'))
        if not self.env.su and (self.filtered(lambda s: s.source_id.source_type == 'energy_charts') or
                                (values.get('source_id') and self.env['grid.tariff.source'].browse(values['source_id']).source_type == 'energy_charts')):
            raise AccessError(_('Energy-Charts intervals can only be changed by syncing the source.'))
        return super().write(values)

    def unlink(self):
        if not self.env.su and self.filtered(lambda s: s.source_id.source_type == 'energy_charts'):
            raise AccessError(_('Energy-Charts intervals can only be changed by syncing the source.'))
        return super().unlink()


class GridTariffSync(models.Model):
    _name = 'grid.tariff.sync'
    _description = 'GridSync Tariff Sync'
    _order = 'started_at desc, id desc'

    source_id = fields.Many2one('grid.tariff.source', required=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(related='source_id.company_id', store=True, index=True)
    provider_type = fields.Selection([('energy_charts', 'Energy-Charts')], required=True)
    started_at = fields.Datetime(required=True)
    next_allowed_at = fields.Datetime(required=True)
    state = fields.Selection([('success', 'Success'), ('failed', 'Failed')], required=True)
    inserted = fields.Integer(readonly=True)
    updated = fields.Integer(readonly=True)
    missing = fields.Integer(readonly=True)
    message = fields.Char(readonly=True)
