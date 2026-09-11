# -*- coding: utf-8 -*-
{
    'name': 'Dex Invoice Cancellation',
    'summary': 'Approval workflows for invoice cancellation and draft invoice updates',
    'description': """
        Adds audited approval workflows for fully crediting posted customer
        invoices and for reducing or deleting lines on draft invoices created
        from sale orders.
    """,
    'version': '13.0.1.0.8',
    'author': 'John Raymark LLavanes',
    'website': 'https://johnraymarksuuuu.github.io/',
    'license': 'LGPL-3',
    'category': 'Accounting',
    'depends': [
        'account',
        'sale',
        'mail',
        'dex_account_move',
        'dex_invoice_creator',
        'dex_return',
        'dex_mail',
    ],
    'data': [
        'security/groups.xml',
        'security/ir.model.access.csv',
        'security/rules.xml',
        'data/sequence.xml',
        'views/res_config_settings_views.xml',
        'views/reason_views.xml',
        'views/request_views.xml',
        'wizard/cancel_request_wizard_views.xml',
        'wizard/update_request_wizard_views.xml',
        'views/account_move_views.xml',
    ],
    'demo': [
        'demo/cancellation_reason_demo.xml',
    ],
    'installable': True,
    'application': False,
}
