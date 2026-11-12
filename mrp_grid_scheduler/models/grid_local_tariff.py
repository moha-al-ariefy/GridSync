import math
from collections import defaultdict

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_compare

from ..services.local_tariffs import MAX_DAYS, WEEKDAYS, TariffInputError, clock_minutes, expand_profile


LOCAL_SOURCE_TYPES = ('manual_tou', 'csv')


class GridTariffSource(models.Model):
    _inherit = 'grid.tariff.source'

    valid_from = fields.Date(help='First local date covered by the documented tariff.')
    valid_to = fields.Date(help='Last local date covered by the documented tariff.')
    rule_ids = fields.One2many('grid.tariff.rule', 'source_id', string='Time-of-use rules')
    profile_dirty = fields.Boolean(readonly=True, copy=False)
    csv_price_basis = fields.Selection([
        ('provided', 'Supplied prices'), ('forecast', 'Forecast'), ('actual', 'Actual'),
    ], default='provided', required=True, string='CSV price type')

    @api.constrains('source_type', 'valid_from', 'valid_to')
    def _check_profile_validity(self):
        for source in self.filtered(lambda source: source.source_type == 'manual_tou'):
            if not source.valid_from or not source.valid_to or source.valid_from > source.valid_to:
                raise ValidationError(_('Enter the first and last dates covered by the tariff.'))

    @api.model_create_multi
    def create(self, values_list):
        if not self.env.su:
            if any('profile_dirty' in values for values in values_list):
                raise AccessError(_('Only GridSync can change the generation status.'))
            values_list = [dict(values, profile_dirty=False) for values in values_list]
        return super().create(values_list)

    def write(self, values):
        if not self.env.su and 'profile_dirty' in values:
            raise AccessError(_('Only GridSync can change the generation status.'))
        profiles = self.filtered(lambda source: source.source_type == 'manual_tou') if (
            {'valid_from', 'valid_to'}.intersection(values)
        ) else self.browse()
        for source in profiles.sorted('id'):
            source.check_access('write')
            source._lock_local_tariff()
        result = super().write(values)
        profiles._invalidate_profile()
        return result

    def _check_local_manager(self):
        self.ensure_one()
        self.check_access('write')
        if not self.env.user.has_group('mrp_grid_scheduler.group_grid_manager'):
            raise AccessError(_('Only a GridSync manager can load tariff prices.'))
        if not self.active or self.source_type not in LOCAL_SOURCE_TYPES:
            raise UserError(_('Choose an active time-of-use or CSV tariff.'))

    def _lock_local_tariff(self):
        self.ensure_one()
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(%s, %s)', (5108844, self.id))
        if not self.env.cr.fetchone()[0]:
            raise UserError(_('This tariff is being updated. Try again shortly.'))

    def _invalidate_profile(self):
        # Rule edits retire generated slots through the same source lock.
        for source in self:
            slots = source.sudo().slot_ids.filtered('is_current')
            slots.write({'is_current': False})
            source.sudo().write({'profile_dirty': True})

    def action_open_tariff_load(self):
        self._check_local_manager()
        action = self.env['ir.actions.actions']._for_xml_id('mrp_grid_scheduler.action_grid_tariff_load')
        action['name'] = _('Generate intervals') if self.source_type == 'manual_tou' else _('Import CSV')
        action['context'] = {'default_source_id': self.id}
        return action

    def _generate_profile_intervals(self, first_day, last_day):
        self._check_local_manager()
        self._lock_local_tariff()
        if self.source_type != 'manual_tou':
            raise UserError(_('Choose a time-of-use tariff.'))
        if not first_day or not last_day or not self.valid_from <= first_day <= last_day <= self.valid_to:
            raise UserError(_('Choose dates within the tariff validity period.'))
        if (last_day - first_day).days + 1 > MAX_DAYS:
            raise UserError(_('Choose at most %s days per generation.', MAX_DAYS))
        rules = [{
            'weekday': rule.weekday, 'date_from': rule.date_from, 'date_to': rule.date_to,
            'start': clock_minutes(rule.start_time), 'end': clock_minutes(rule.end_time, end=True),
            'price': round(rule.price_per_kwh * 1000, 6),
        } for rule in self.rule_ids]
        try:
            rows, missing = expand_profile(rules, self.timezone, first_day, last_day)
        except TariffInputError as exc:
            raise UserError(str(exc)) from exc
        return self._save_local_intervals(rows, missing, replace_existing=True)

    def _save_local_intervals(self, rows, missing, replace_existing=False):
        self._check_local_manager()
        self._lock_local_tariff()
        now = fields.Datetime.now()
        # Only this manager-checked operation can write derived intervals and log metadata.
        slots = self.env['grid.tariff.slot'].sudo()
        existing = slots.search([('source_id', '=', self.id), ('start_utc', 'in', [start for start, _ in rows])])
        by_start = {slot.start_utc: slot for slot in existing}
        new_slots = []
        updates = defaultdict(list)
        unchanged = 0
        for start, price in rows:
            slot = by_start.get(start)
            if not slot:
                new_slots.append({'source_id': self.id, 'start_utc': start, 'price_per_mwh': price, 'observed_at': now})
            elif slot.is_current and float_compare(slot.price_per_mwh, price, precision_digits=6) == 0:
                unchanged += 1
            elif not replace_existing:
                raise UserError(_('An existing interval has a different price at %s UTC. Enable replacement to update it.', start))
            else:
                updates[price].append(slot.id)
        if new_slots:
            slots.create(new_slots)
        for price, ids in updates.items():
            slots.browse(ids).write({'price_per_mwh': price, 'observed_at': now, 'is_current': True})
        updated = len(existing) - unchanged
        self.sudo().write({'last_sync_at': now, 'profile_dirty': False})
        self.env['grid.tariff.sync'].sudo().create({
            'source_id': self.id, 'provider_type': self.source_type, 'started_at': now, 'next_allowed_at': now,
            'state': 'success', 'inserted': len(new_slots), 'updated': updated, 'missing': missing,
        })
        message = _('%(new)s added, %(updated)s updated, %(same)s unchanged, %(missing)s missing.',
                    new=len(new_slots), updated=updated, same=unchanged, missing=missing)
        return self._sync_notice(message, 'warning' if missing else 'success')


class GridTariffRule(models.Model):
    _name = 'grid.tariff.rule'
    _description = 'GridSync Time-of-use Rule'
    _check_company_auto = True
    _order = 'date_from, weekday, start_time, id'

    source_id = fields.Many2one('grid.tariff.source', required=True, ondelete='cascade', check_company=True, index=True)
    company_id = fields.Many2one(related='source_id.company_id', store=True, index=True)
    weekday = fields.Selection(WEEKDAYS, required=True, default='all', string='Days')
    start_time = fields.Char(required=True, default='00:00', string='From')
    end_time = fields.Char(required=True, default='24:00', string='Until')
    date_from = fields.Date(string='Season starts')
    date_to = fields.Date(string='Season ends')
    price_per_kwh = fields.Float(required=True, digits=(13, 6), string='Price per kWh')

    @api.constrains('source_id', 'start_time', 'end_time', 'date_from', 'date_to', 'price_per_kwh')
    def _check_rule(self):
        for rule in self:
            try:
                if clock_minutes(rule.start_time) >= clock_minutes(rule.end_time, end=True):
                    raise TariffInputError('The end must follow the start. Split overnight rules at midnight.')
            except TariffInputError as exc:
                raise ValidationError(str(exc)) from exc
            if not math.isfinite(rule.price_per_kwh):
                raise ValidationError(_('The price must be a finite number.'))
            if rule.source_id.source_type != 'manual_tou':
                raise ValidationError(_('Rules belong to a time-of-use tariff.'))
            if bool(rule.date_from) != bool(rule.date_to) or (rule.date_from and rule.date_from > rule.date_to):
                raise ValidationError(_('Enter both season dates in order, or leave both blank.'))

    def _lock_profiles(self, sources):
        for source in sources.sorted('id'):
            source.check_access('write')
            source._lock_local_tariff()

    @api.model_create_multi
    def create(self, values_list):
        if not self.env.su:
            if any('company_id' in values for values in values_list):
                raise AccessError(_('The rule company comes from its tariff source.'))
            context = dict(self.env.context)
            context.pop('default_company_id', None)
            self = self.with_context(context)
        default_source = self.default_get(['source_id']).get('source_id')
        source_ids = {values.get('source_id', default_source) for values in values_list}
        sources = self.env['grid.tariff.source'].browse(sorted(source_ids - {None, False}))
        self._lock_profiles(sources)
        rules = super().create(values_list)
        rules.source_id._invalidate_profile()
        return rules

    def write(self, values):
        self.check_access('write')
        if not self.env.su and 'company_id' in values:
            raise AccessError(_('The rule company comes from its tariff source.'))
        if 'source_id' in values:
            raise UserError(_('Create a new rule to move it to another tariff.'))
        sources = self.source_id
        self._lock_profiles(sources)
        result = super().write(values)
        sources._invalidate_profile()
        return result

    def unlink(self):
        self.check_access('unlink')
        sources = self.source_id
        self._lock_profiles(sources)
        result = super().unlink()
        sources._invalidate_profile()
        return result
