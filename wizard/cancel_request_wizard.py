# -*- coding: utf-8 -*-

from odoo import _, fields, models
from odoo.exceptions import UserError


class InvoiceCancelRequestWizard(models.TransientModel):
    _name = 'dex.invoice.cancel.request.wizard'
    _description = 'Request Invoice Cancellation'

    invoice_id = fields.Many2one('account.move', required=True, readonly=True)
    sale_order_id = fields.Many2one(
        'sale.order', related='invoice_id.sale_order_id', readonly=True)
    reason_id = fields.Many2one(
        'dex.invoice.cancel.reason', string='Cancellation Reason', required=True,
        domain=[('active', '=', True)])
    justification = fields.Text(string='Remarks', required=True)

    def action_submit(self):
        self.ensure_one()
        if not (self.justification or '').strip():
            raise UserError(_('Remarks are required.'))
        request = self.env['dex.invoice.cancel.request'].create({
            'invoice_id': self.invoice_id.id,
            'reason_id': self.reason_id.id,
            'justification': self.justification.strip(),
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Invoice Cancellation Request'),
            'res_model': 'dex.invoice.cancel.request',
            'res_id': request.id,
            'view_mode': 'form',
            'target': 'current',
        }
