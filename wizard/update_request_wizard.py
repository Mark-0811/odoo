# -*- coding: utf-8 -*-

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare


class InvoiceUpdateRequestWizard(models.TransientModel):
    _name = 'dex.invoice.update.request.wizard'
    _description = 'Request Invoice Update'

    invoice_id = fields.Many2one('account.move', required=True, readonly=True)
    sale_order_id = fields.Many2one(
        'sale.order', related='invoice_id.sale_order_id', readonly=True)
    current_invoice_date = fields.Date(
        related='invoice_id.invoice_date', string='Current Invoice Date', readonly=True)
    change_invoice_date = fields.Boolean(string='Change Invoice Date')
    requested_invoice_date = fields.Date(string='Requested Invoice Date')
    justification = fields.Text(string='Remarks')
    line_ids = fields.One2many(
        'dex.invoice.update.request.wizard.line', 'wizard_id', string='Invoice Lines')

    @api.onchange('change_invoice_date')
    def _onchange_change_invoice_date(self):
        if not self.change_invoice_date:
            self.requested_invoice_date = False

    def action_submit(self):
        self.ensure_one()
        self.invoice_id._dex_update_request_eligibility()
        if not (self.justification or '').strip():
            raise UserError(_('Remarks are required.'))
        if self.change_invoice_date:
            if not self.requested_invoice_date:
                raise UserError(_('Requested Invoice Date is required.'))
            if self.requested_invoice_date == self.current_invoice_date:
                raise UserError(_('Requested Invoice Date must differ from the current date.'))

        request_lines = []
        for line in self.line_ids.filtered(lambda item: item.change_type != 'none'):
            rounding = line.product_uom_id.rounding or 0.01
            if line.change_type == 'reduce':
                if float_compare(line.new_quantity, 0, precision_rounding=rounding) <= 0:
                    raise UserError(_('Reduced quantities must remain greater than zero.'))
                if float_compare(
                        line.new_quantity, line.current_quantity,
                        precision_rounding=rounding) >= 0:
                    raise UserError(_('Requested quantities must be lower than current quantities.'))
            request_lines.append((0, 0, {
                'invoice_line_id': line.invoice_line_id.id,
                'product_id': line.product_id.id,
                'description': line.description,
                'product_uom_id': line.product_uom_id.id,
                'original_quantity': line.current_quantity,
                'change_type': line.change_type,
                'target_quantity': line.new_quantity if line.change_type == 'reduce' else 0,
            }))

        if not self.change_invoice_date and not request_lines:
            raise UserError(_('Request at least one invoice date or line change.'))

        request = self.env['dex.invoice.update.request'].create({
            'invoice_id': self.invoice_id.id,
            'change_invoice_date': self.change_invoice_date,
            'requested_invoice_date': (
                self.requested_invoice_date if self.change_invoice_date else False),
            'justification': self.justification.strip(),
            'line_ids': request_lines,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Invoice Update Request'),
            'res_model': 'dex.invoice.update.request',
            'res_id': request.id,
            'view_mode': 'form',
            'target': 'current',
        }


class InvoiceUpdateRequestWizardLine(models.TransientModel):
    _name = 'dex.invoice.update.request.wizard.line'
    _description = 'Invoice Update Request Wizard Line'
    _order = 'id'

    wizard_id = fields.Many2one(
        'dex.invoice.update.request.wizard', required=True, ondelete='cascade')
    invoice_line_id = fields.Many2one('account.move.line', required=True, readonly=True)
    product_id = fields.Many2one('product.product', readonly=True)
    description = fields.Char(readonly=True)
    product_uom_id = fields.Many2one('uom.uom', string='Unit of Measure', readonly=True)
    current_quantity = fields.Float(
        string='Current Quantity', digits='Product Unit of Measure', readonly=True)
    change_type = fields.Selection([
        ('none', 'No Change'),
        ('reduce', 'Reduce Quantity'),
        ('delete', 'Delete Line'),
    ], string='Requested Action', required=True, default='none')
    new_quantity = fields.Float(
        string='Requested Quantity', digits='Product Unit of Measure')

    @api.onchange('change_type')
    def _onchange_change_type(self):
        if self.change_type == 'none':
            self.new_quantity = self.current_quantity
        elif self.change_type == 'delete':
            self.new_quantity = 0
