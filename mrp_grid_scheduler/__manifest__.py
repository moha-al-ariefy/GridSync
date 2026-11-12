{
    'name': 'GridSync MRP',
    'version': '19.0.1.0.0',
    'summary': 'Plan manufacturing around energy prices',
    'category': 'Manufacturing',
    'depends': ['mrp'],
    'data': [
        'security/grid_security.xml',
        'security/ir.model.access.csv',
        'views/grid_site_views.xml',
    ],
    'assets': {
        'web.assets_backend': ['mrp_grid_scheduler/static/src/scss/grid_sync.scss'],
    },
    'application': True,
    'installable': True,
    'license': 'LGPL-3',
}
