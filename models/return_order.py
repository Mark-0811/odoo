# -*- coding: utf-8 -*-

from odoo import _, models
from odoo.exceptions import ValidationError


class SaleStockReturn(models.Model):
    _inherit = 'sale.stock.return'

    def _check_return_order(self):
        result = super(SaleStockReturn, self)._check_return_order()
        for order in self:
            blocked_invoices = order.return_line.mapped('invoice_id.move_id').filtered(
                'dex_cancelled_by_request')
            if blocked_invoices:
                raise ValidationError(_(
                    'Invoices cancelled through an approved cancellation request '
                    'cannot be used in a return order: %s') %
                    ', '.join(blocked_invoices.mapped('display_name')))
        return result
