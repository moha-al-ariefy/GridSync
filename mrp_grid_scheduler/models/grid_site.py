from odoo import fields, models


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

    _name_company_unique = models.Constraint(
        'UNIQUE(name, company_id)', 'A site with this name already exists in this company.',
    )
