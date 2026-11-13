from odoo import fields, models

from ..services.scheduler import MAX_JOBS


class ResCompany(models.Model):
    _inherit = 'res.company'

    grid_auto_sync = fields.Boolean(
        string='Automatic tariff sync', default=True, groups='base.group_system',
    )
    grid_queue_threshold = fields.Integer(
        'Operations before background scheduling', default=500, required=True, groups='base.group_system',
    )

    _grid_queue_threshold_range = models.Constraint(
        f'CHECK(grid_queue_threshold BETWEEN 0 AND {MAX_JOBS})',
        lambda env, _diag: env._('Choose a background scheduling threshold from 0 to %s.', MAX_JOBS),
    )


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    grid_auto_sync = fields.Boolean(related='company_id.grid_auto_sync', readonly=False)
    grid_queue_threshold = fields.Integer(related='company_id.grid_queue_threshold', readonly=False)
