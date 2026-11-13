import math

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


GRID_PLANNER = 'mrp_grid_scheduler.group_grid_planner'
GRID_MANAGER = 'mrp_grid_scheduler.group_grid_manager'
ENERGY_FIELDS = {'grid_power_kw', 'grid_power_reference', 'grid_tariff_source_id', 'grid_allow_shift',
                 'grid_use_carbon_factor', 'grid_carbon_factor', 'grid_carbon_reference'}


class MrpWorkcenter(models.Model):
    _inherit = 'mrp.workcenter'

    grid_power_kw = fields.Float(
        string='Operating power (kW)', digits=(12, 3), copy=False, groups=GRID_PLANNER,
        help='Typical power while running. Use a documented value to estimate electricity costs.',
    )
    grid_power_reference = fields.Char(
        string='Power reference', copy=False, groups=GRID_PLANNER,
        help='For example, a meter reading or equipment specification.',
    )
    grid_tariff_source_id = fields.Many2one(
        'grid.tariff.source', string='Electricity tariff', check_company=True, ondelete='restrict',
        copy=False, index=True, groups=GRID_PLANNER,
    )
    grid_allow_shift = fields.Boolean(
        string='Allow schedule suggestions', copy=False, groups=GRID_PLANNER,
        help='Include this work center in GridSync scheduling suggestions.',
    )
    grid_use_carbon_factor = fields.Boolean('Estimate emissions from a documented factor', groups=GRID_PLANNER, copy=False)
    grid_carbon_factor = fields.Float('Electricity factor (g CO₂e/kWh)', digits=(12, 6), groups=GRID_PLANNER, copy=False)
    grid_carbon_reference = fields.Char('Electricity factor source', groups=GRID_PLANNER, copy=False)
    grid_energy_can_edit = fields.Boolean(compute='_compute_grid_energy_can_edit', groups=GRID_PLANNER)

    @api.depends_context('uid')
    def _compute_grid_energy_can_edit(self):
        can_edit = self.env.user.has_group(GRID_MANAGER)
        for workcenter in self:
            workcenter.grid_energy_can_edit = can_edit

    def _check_grid_energy_access(self, field_names):
        if not self.env.su and ENERGY_FIELDS.intersection(field_names) and not self.env.user.has_group(GRID_MANAGER):
            raise AccessError(_('Only a GridSync manager can edit work center energy settings.'))

    @api.model_create_multi
    def create(self, values_list):
        self._check_grid_energy_access(name for values in values_list for name in values)
        self._check_grid_energy_access(name for name in ENERGY_FIELDS if f'default_{name}' in self.env.context)
        workcenters = super().create(values_list)
        if self.env.su or self.env.user.has_group(GRID_MANAGER):
            workcenters._record_tariff_link()
        return workcenters

    def write(self, values):
        self._check_grid_energy_access(values)
        result = super().write(values)
        if values.get('grid_tariff_source_id'):
            self._record_tariff_link()
        return result

    def _record_tariff_link(self):
        tariffs = self.grid_tariff_source_id
        tariffs.check_access('read')
        # A tariff move and a new link must update the same row in one transaction.
        tariffs.write({'write_date': fields.Datetime.now()})

    @api.constrains('grid_use_carbon_factor', 'grid_carbon_factor', 'grid_carbon_reference')
    def _check_grid_carbon_factor(self):
        for center in self.sudo().filtered('grid_use_carbon_factor'):
            if not 0 <= center.grid_carbon_factor <= 10_000 or not (center.grid_carbon_reference or '').strip():
                raise ValidationError(_('Enter a non-negative electricity factor up to 10,000 g CO₂e/kWh and its source.'))

    @api.constrains('grid_power_kw', 'grid_tariff_source_id', 'grid_allow_shift', 'company_id')
    def _check_grid_energy_settings(self):
        # Manufacturing users can still edit ordinary fields even if they cannot read the GridSync fields.
        for workcenter in self.sudo():
            power = workcenter.grid_power_kw
            if not math.isfinite(power) or power < 0:
                raise ValidationError(_('Operating power must be a finite, non-negative number.'))
            tariff = workcenter.grid_tariff_source_id
            if tariff and tariff.company_id != workcenter.company_id:
                raise ValidationError(_('The work center and its electricity tariff must belong to the same company.'))
            if workcenter.grid_allow_shift and (power <= 0 or not tariff.active):
                raise ValidationError(_('Set operating power and choose an active tariff before allowing schedule suggestions.'))


class GridTariffSource(models.Model):
    _inherit = 'grid.tariff.source'

    def write(self, values):
        moving = self.browse()
        if 'company_id' in values:
            self.check_access('write')
            moving = self.filtered(lambda tariff: tariff.company_id.id != values['company_id'])
        result = super().write(values)
        # Include archived work centers when checking existing tariff links.
        if moving and self.env['mrp.workcenter'].sudo().with_context(active_test=False).search_count([
            ('grid_tariff_source_id', 'in', moving.ids),
        ], limit=1):
            raise UserError(_('Remove the work center links before moving this tariff to another company.'))
        return result
