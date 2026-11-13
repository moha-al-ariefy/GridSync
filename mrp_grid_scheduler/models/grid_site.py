from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class GridSite(models.Model):
    _name = 'grid.site'
    _description = 'GridSync Site'
    _check_company_auto = True
    _order = 'name, id'

    name = fields.Char(required=True, translate=False)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company, index=True,
    )
    workcenter_ids = fields.Many2many(
        'mrp.workcenter', string='Work Centers', check_company=True,
    )
    active = fields.Boolean(default=True)
    tariff_ids = fields.Many2many('grid.tariff.source', compute='_compute_tariffs', string='Tariffs')

    _name_company_unique = models.Constraint(
        'UNIQUE(name, company_id)', 'A site with this name already exists in this company.',
    )

    @api.depends('workcenter_ids.grid_tariff_source_id')
    def _compute_tariffs(self):
        for site in self:
            site.tariff_ids = site.workcenter_ids.grid_tariff_source_id

    def action_open_schedules(self):
        self.ensure_one()
        self.check_access('read')
        centers = self.workcenter_ids
        action = self.env['ir.actions.actions']._for_xml_id('mrp_grid_scheduler.action_grid_schedule_run')
        action['domain'] = [('company_id', '=', self.company_id.id), ('workcenter_ids', 'in', centers.ids)]
        return action

    def action_open_energy_results(self):
        self.ensure_one()
        self.check_access('read')
        action = self.env['ir.actions.actions']._for_xml_id('mrp_grid_scheduler.action_grid_energy_results')
        action['domain'] = [
            ('company_id', '=', self.company_id.id),
            ('line_id.workcenter_id', 'in', self.workcenter_ids.ids),
        ]
        return action

    def write(self, values):
        if not self.env.su and values.get('company_id') and values['company_id'] not in self.env.companies.ids:
            raise AccessError(_('The selected company is not available to this user.'))
        return super().write(values)
