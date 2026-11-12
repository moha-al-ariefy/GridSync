from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    grid_auto_sync = fields.Boolean(string='Automatic tariff sync', default=True)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    grid_auto_sync = fields.Boolean(related='company_id.grid_auto_sync', readonly=False)
