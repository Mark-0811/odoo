# -*- coding: utf-8 -*-

from odoo import _, api, fields, models
from odoo.exceptions import UserError


_MOVE_CREATE_SENTINEL = object()
_SALE_INVOICE_CREATE_SENTINEL = object()
_UPDATE_APPLY_SENTINEL = object()
_CANCEL_APPLY_SENTINEL = object()
_POST_SENTINEL = object()


class AccountMove(models.Model):
    _inherit = 'account.move'

    dex_cancel_request_ids = fields.One2many(
        'dex.invoice.cancel.request', 'invoice_id', string='Cancellation Requests', copy=False)
    dex_cancel_request_count = fields.Integer(
        string='Cancellation Request Count', compute='_compute_dex_request_counts',
        compute_sudo=True)
    dex_update_request_ids = fields.One2many(
        'dex.invoice.update.request', 'invoice_id', string='Update Requests', copy=False)
    dex_update_request_count = fields.Integer(
        string='Update Request Count', compute='_compute_dex_request_counts',
        compute_sudo=True)
    dex_cancelled_by_request = fields.Boolean(
        string='Cancelled by Request', copy=False, readonly=True, tracking=True, index=True)
    dex_cancel_credit_move_id = fields.Many2one(
        'account.move', string='Cancellation Credit Memo', copy=False, readonly=True)
    dex_cancel_request_pending = fields.Boolean(
        string='Cancellation Request Pending', compute='_compute_dex_request_state',
        compute_sudo=True)
    dex_update_request_pending = fields.Boolean(
        string='Update Request Pending', compute='_compute_dex_request_state',
        compute_sudo=True)
    dex_sale_invoice_edit_locked = fields.Boolean(
        string='Sale Invoice Editing Locked', compute='_compute_dex_request_state',
        compute_sudo=True)

    @api.depends('dex_cancel_request_ids', 'dex_update_request_ids')
    def _compute_dex_request_counts(self):
        for move in self:
            move.dex_cancel_request_count = len(move.dex_cancel_request_ids)
            move.dex_update_request_count = len(move.dex_update_request_ids)

    @api.depends(
        'type', 'state', 'sale_order_id', 'dex_cancel_request_ids.state',
        'dex_update_request_ids.state')
    def _compute_dex_request_state(self):
        for move in self:
            move.dex_cancel_request_pending = any(
                request.state == 'requested' for request in move.dex_cancel_request_ids)
            move.dex_update_request_pending = any(
                request.state == 'requested' for request in move.dex_update_request_ids)
            move.dex_sale_invoice_edit_locked = bool(
                move.type == 'out_invoice' and move.state == 'draft' and move.sale_order_id)

    def _dex_cancel_request_eligibility(self, current_request=False):
        self.ensure_one()
        invoice = self.sudo()
        if invoice.type != 'out_invoice':
            raise UserError(_('Only customer invoices can be cancelled through this workflow.'))
        if invoice.state != 'posted':
            raise UserError(_('The customer invoice must be posted.'))
        if not invoice.sale_order_id:
            raise UserError(_('The customer invoice must be linked to a sale order.'))
        if invoice.dex_cancelled_by_request:
            raise UserError(_('This invoice was already cancelled through an approved request.'))
        current_request_id = current_request.id if current_request else False
        pending_requests = invoice.dex_cancel_request_ids.filtered(
            lambda request: request.state == 'requested'
            and request.id != current_request_id)
        if pending_requests:
            raise UserError(_('This invoice already has a pending cancellation request.'))
        if not invoice.currency_id.is_zero(invoice.applied_payment):
            raise UserError(_('Cancel the applied payment before requesting invoice cancellation.'))
        if (invoice.whtax_payment_count or
                not invoice.currency_id.is_zero(invoice.applied_whtax)):
            raise UserError(_('Cancel the withholding tax before requesting invoice cancellation.'))
        if not invoice.currency_id.is_zero(invoice.amount_total - invoice.amount_residual):
            raise UserError(_('The invoice must have its full amount outstanding.'))
        if invoice.reinvoice_ids_count:
            raise UserError(_('Cancel the pending or posted reinvoice request first.'))

        return_lines = self.env['sale.stock.return.line'].sudo().search_count([
            ('invoice_id.move_id', '=', invoice.id),
            ('return_id.state', '!=', 'cancel'),
        ])
        if return_lines:
            raise UserError(_('This invoice was already used in a return order.'))

        credit_memos = self.env['account.move'].sudo().search_count([
            ('type', '=', 'out_refund'),
            ('state', '!=', 'cancel'),
            '|',
            ('reversed_entry_id', '=', invoice.id),
            ('source_move_id', '=', invoice.id),
        ])
        if (credit_memos or
                not invoice.currency_id.is_zero(invoice.applied_credit_memo)):
            raise UserError(_('This invoice already has credit memo activity.'))
        return True

    def _dex_update_request_eligibility(self, current_request=False):
        self.ensure_one()
        invoice = self.sudo()
        if invoice.type != 'out_invoice':
            raise UserError(_('Only customer invoices can be updated through this workflow.'))
        if invoice.state != 'draft':
            raise UserError(_('Only draft customer invoices can be updated.'))
        if not invoice.sale_order_id:
            raise UserError(_('The draft invoice must be linked to a sale order.'))
        current_request_id = current_request.id if current_request else False
        pending_requests = invoice.dex_update_request_ids.filtered(
            lambda request: request.state == 'requested'
            and request.id != current_request_id)
        if pending_requests:
            raise UserError(_('This invoice already has a pending update request.'))
        return True

    def _dex_update_context_allowed(self):
        self.ensure_one()
        if self.env.context.get('dex_invoice_update_internal') is not _UPDATE_APPLY_SENTINEL:
            return False
        request_id = self.env.context.get('dex_invoice_update_request_id')
        if not request_id:
            return False
        request = self.env['dex.invoice.update.request'].sudo().browse(request_id).exists()
        return bool(
            request and request.state == 'requested'
            and request.invoice_id.id == self.id)

    @api.model_create_multi
    def create(self, vals_list):
        if any(
                {'dex_cancelled_by_request', 'dex_cancel_credit_move_id'} & set(vals)
                for vals in vals_list):
            raise UserError(_(
                'Cancellation audit fields can only be set by an approved request.'))
        creation_self = self.with_context(dex_invoice_move_initial_create=_MOVE_CREATE_SENTINEL)
        moves = super(AccountMove, creation_self).create(vals_list)
        return moves.with_context(dex_invoice_move_initial_create=None)

    def write(self, vals):
        protected = {'dex_cancelled_by_request', 'dex_cancel_credit_move_id'} & set(vals)
        if (protected and
                self.env.context.get('dex_invoice_cancel_internal') is not _CANCEL_APPLY_SENTINEL):
            raise UserError(_(
                'Cancellation audit fields can only be set by an approved request.'))
        if ('invoice_date' in vals and
                self.env.context.get('dex_invoice_move_initial_create') is not _MOVE_CREATE_SENTINEL):
            for move in self.filtered(
                    lambda item: item.type == 'out_invoice'
                    and item.state == 'draft' and item.sale_order_id):
                if not move._dex_update_context_allowed():
                    raise UserError(_(
                        'Use an approved Invoice Update Request to change the invoice date.'))
                request = self.env['dex.invoice.update.request'].browse(
                    self.env.context['dex_invoice_update_request_id'])
                if (not request.change_invoice_date or
                        vals['invoice_date'] != request.requested_invoice_date):
                    raise UserError(_('Only the approved invoice date may be applied.'))
        return super(AccountMove, self).write(vals)

    def action_post(self):
        pending = self.filtered('dex_update_request_pending')
        if pending:
            raise UserError(_(
                'Resolve the pending Invoice Update Request before posting the invoice.'))
        return super(AccountMove, self).action_post()

    def post(self):
        pending = self.filtered('dex_update_request_pending')
        if pending:
            raise UserError(_(
                'Resolve the pending Invoice Update Request before posting the invoice.'))
        posting_self = self.with_context(dex_invoice_post_internal=_POST_SENTINEL)
        return super(AccountMove, posting_self).post()

    def action_open_dex_cancel_request_wizard(self):
        self.ensure_one()
        self._dex_cancel_request_eligibility()
        view = self.env.ref('dex_invoice_cancel.view_cancel_request_wizard_form')
        return {
            'type': 'ir.actions.act_window',
            'name': _('Request Invoice Cancellation'),
            'res_model': 'dex.invoice.cancel.request.wizard',
            'view_mode': 'form',
            'views': [(view.id, 'form')],
            'target': 'new',
            'context': {'default_invoice_id': self.id},
        }

    def action_open_dex_update_request_wizard(self):
        self.ensure_one()
        self._dex_update_request_eligibility()
        wizard = self.env['dex.invoice.update.request.wizard'].create({
            'invoice_id': self.id,
            'line_ids': [(0, 0, {
                'invoice_line_id': line.id,
                'product_id': line.product_id.id,
                'description': line.name,
                'current_quantity': line.quantity,
                'new_quantity': line.quantity,
                'product_uom_id': line.product_uom_id.id,
            }) for line in self.invoice_line_ids.filtered(lambda line: not line.display_type)],
        })
        view = self.env.ref('dex_invoice_cancel.view_update_request_wizard_form')
        return {
            'type': 'ir.actions.act_window',
            'name': _('Request Invoice Update'),
            'res_model': 'dex.invoice.update.request.wizard',
            'res_id': wizard.id,
            'view_mode': 'form',
            'views': [(view.id, 'form')],
            'target': 'new',
        }

    def action_view_dex_cancel_requests(self):
        self.ensure_one()
        action = self.env.ref('dex_invoice_cancel.action_invoice_cancel_requests').read()[0]
        action['domain'] = [('invoice_id', '=', self.id)]
        action['context'] = {'default_invoice_id': self.id}
        return action

    def action_view_dex_update_requests(self):
        self.ensure_one()
        action = self.env.ref('dex_invoice_cancel.action_invoice_update_requests').read()[0]
        action['domain'] = [('invoice_id', '=', self.id)]
        action['context'] = {'default_invoice_id': self.id}
        return action


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    def _dex_locked_sale_invoice_lines(self):
        return self.filtered(
            lambda line: not line.exclude_from_invoice_tab
            and line.move_id.type == 'out_invoice'
            and line.move_id.state == 'draft'
            and line.move_id.sale_order_id)

    def _dex_check_sale_invoice_edit(self):
        if (self.env.context.get('dex_invoice_move_initial_create') is _MOVE_CREATE_SENTINEL or
                self.env.context.get('dex_invoice_sale_creation') is _SALE_INVOICE_CREATE_SENTINEL or
                self.env.context.get('dex_invoice_post_internal') is _POST_SENTINEL):
            return
        for move in self._dex_locked_sale_invoice_lines().mapped('move_id'):
            if not move._dex_update_context_allowed():
                raise UserError(_(
                    'Use an approved Invoice Update Request to change sale invoice lines.'))

    @api.model_create_multi
    def create(self, vals_list):
        if (self.env.context.get('dex_invoice_move_initial_create') is not _MOVE_CREATE_SENTINEL and
                self.env.context.get('dex_invoice_sale_creation') is not _SALE_INVOICE_CREATE_SENTINEL and
                self.env.context.get('dex_invoice_post_internal') is not _POST_SENTINEL):
            move_ids = [vals.get('move_id') for vals in vals_list if vals.get('move_id')]
            moves = self.env['account.move'].browse(move_ids).exists()
            for move in moves.filtered(
                    lambda item: item.type == 'out_invoice'
                    and item.state == 'draft' and item.sale_order_id):
                if not move._dex_update_context_allowed():
                    raise UserError(_(
                        'Use an approved Invoice Update Request to add sale invoice lines.'))
        return super(AccountMoveLine, self).create(vals_list)

    def write(self, vals):
        if (self.env.context.get('dex_invoice_move_initial_create') is _MOVE_CREATE_SENTINEL or
                self.env.context.get('dex_invoice_sale_creation') is _SALE_INVOICE_CREATE_SENTINEL or
                self.env.context.get('dex_invoice_post_internal') is _POST_SENTINEL):
            return super(AccountMoveLine, self).write(vals)
        self._dex_check_sale_invoice_edit()
        return super(AccountMoveLine, self).write(vals)

    def unlink(self):
        self._dex_check_sale_invoice_edit()
        return super(AccountMoveLine, self).unlink()


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def action_create_multi_invoices(self, multi_lines, counts):
        creation_self = self.with_context(
            dex_invoice_sale_creation=_SALE_INVOICE_CREATE_SENTINEL)
        return super(SaleOrder, creation_self).action_create_multi_invoices(
            multi_lines, counts)
