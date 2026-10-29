{
    'name': 'Rekhu Builder',
    'summary': 'An AI agent for Odoo developers: reads the database as it runs, designs and builds your custom modules on a git branch, tests and upgrades them',
    'version': '17.0.1.0.6',
    'category': 'Productivity',
    'author': 'Rekhu',
    'website': 'https://rekhu.com/products/odoo',
    'support': 'hello@rekhu.com',
    'images': ['static/description/banner.png'],
    'license': 'Other proprietary',
    'depends': ['web'],
    'external_dependencies': {'python': ['requests']},
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/res_config_settings_views.xml',
    ],
}
