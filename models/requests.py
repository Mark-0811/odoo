# -*- coding: utf-8 -*-

import logging
from ast import literal_eval

from markupsafe import escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare

from .account_move import _CANCEL_APPLY_SENTINEL, _UPDATE_APPLY_SENTINEL


_logger = logging.getLogger(__name__)
_REQUEST_WRITE_SENTINEL = object()
_REQUEST_LINE_CREATE_SENTINEL = object()


class InvoiceCancelReason(models.Model):
    _name = 'dex.invoice.cancel.reason'
    _description = 'Invoice Cancellation Reason'
    _order = 'sequence, name, id'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('name_uniq', 'unique(name)', 'The cancellation reason must be unique.'),
    ]

    def unlink(self):
        if not self.env.user.has_group('base.group_system'):
            raise UserError(_('Archive cancellation reasons instead of deleting them.'))
        return super(InvoiceCancelReason, self).unlink()


class InvoiceRequestMixin(models.AbstractModel):
    _name = 'dex.invoice.request.mixin'
    _description = 'Invoice Approval Request Mixin'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(
        string='Reference', required=True, readonly=True, copy=False, default='New', index=True)
    invoice_id = fields.Many2one(
        'account.move', string='Invoice', required=True, readonly=True, copy=False,
        ondelete='restrict', index=True)
    sale_order_id = fields.Many2one(
        'sale.order', string='Sale Order', required=True, readonly=True,
        ondelete='restrict')
    partner_id = fields.Many2one(
        'res.partner', string='Customer', required=True, readonly=True,
        ondelete='restrict')
    currency_id = fields.Many2one(
        'res.currency', required=True, readonly=True, ondelete='restrict')
    amount_total = fields.Monetary(
        string='Invoice Total', currency_field='currency_id', required=True,
        readonly=True)
    justification = fields.Text(string='Remarks', required=True, readonly=True)
    state = fields.Selection([
        ('requested', 'Requested'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], default='requested', required=True, readonly=True, copy=False, tracking=True, index=True)
    requested_by_id = fields.Many2one(
        'res.users', string='Requested By', required=True, readonly=True, copy=False,
        default=lambda self: self.env.user)
    requested_date = fields.Datetime(
        string='Requested On', required=True, readonly=True, copy=False,
        default=fields.Datetime.now)
    decided_by_id = fields.Many2one(
        'res.users', string='Approved or Rejected By', readonly=True, copy=False)
    decided_date = fields.Datetime(
        string='Approved or Rejected On', readonly=True, copy=False)
    decision_remarks = fields.Text(string='Decision Remarks', copy=False)
    request_mail_sent = fields.Boolean(string='Request Email Sent', readonly=True, copy=False)

    _approver_group_xmlid = False
    _approver_config_parameter = False
    _request_label = 'Invoice Request'

    def _approver_users(self):
        self.ensure_one()
        parameters = self.env['ir.config_parameter'].sudo()
        configured = parameters.get_param(self._approver_config_parameter)
        users = self.env['res.users'].sudo()
        if configured:
            try:
                user_ids = literal_eval(configured)
                if isinstance(user_ids, (list, tuple)):
                    users = users.browse([int(user_id) for user_id in user_ids])
            except (SyntaxError, ValueError, TypeError):
                _logger.exception(
                    'Invalid configured approver list for %s', self._name)
        if not users:
            users = self.env.ref(self._approver_group_xmlid).sudo().users
        return users.filtered(lambda user: user.active and not user.share)

    @api.model
    def _emails_for_users(self, users):
        emails = {}
        for user in users:
            email = user.email or user.partner_id.email
            if email:
                email = email.strip()
                if email:
                    emails.setdefault(email.lower(), email)
        return sorted(emails.values(), key=lambda email: email.lower())

    def _request_url(self):
        self.ensure_one()
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        return '%s/web#id=%s&view_type=form&model=%s' % (
            base_url, self.id, self._name)

    def _request_email_details(self):
        self.ensure_one()
        return ''

    def _send_email_immediate(self, recipients, subject, body):
        self.ensure_one()
        return self.env['dex_mail.mail'].send_immediate(
            recipients=', '.join(recipients),
            subject=subject,
            html_text=body,
        )

    def _send_request_email(self):
        self.ensure_one()
        recipients = self._emails_for_users(self._approver_users())
        if not recipients:
            raise UserError(_(
                'No active approver has a valid email address. The request was not submitted.'))
        subject = _('%s Approval Needed: %s') % (
            self._request_label, self.invoice_id.display_name)
        body = """
            <html><body style="font-family: Arial, sans-serif; font-size: 14px; color: #000;">
                <p>{label} approval is requested.</p>
                <p><strong>Request:</strong> {request}<br/>
                   <strong>Invoice:</strong> {invoice}<br/>
                   <strong>Sale Order:</strong> {sale_order}<br/>
                   <strong>Customer:</strong> {customer}<br/>
                   <strong>Requested By:</strong> {requested_by}<br/>
                   <strong>Remarks:</strong> {remarks}</p>
                {details}
                <p><a href="{url}">View Request in Odoo</a></p>
            </body></html>
        """.format(
            label=escape(self._request_label),
            request=escape(self.name or ''),
            invoice=escape(self.invoice_id.display_name or ''),
            sale_order=escape(self.sale_order_id.display_name or ''),
            customer=escape(self.partner_id.display_name or ''),
            requested_by=escape(self.requested_by_id.name or ''),
            remarks=escape(self.justification or ''),
            details=self._request_email_details(),
            url=escape(self._request_url()),
        )
        message_id = self._send_email_immediate(recipients, subject, body)
        if not message_id:
            raise UserError(_('The approver email could not be sent. The request was not submitted.'))
        self._internal_write({
            'request_mail_sent': True,
        })

    def _send_decision_email(self):
        self.ensure_one()
        sudo_request = self.sudo()
        users = sudo_request.requested_by_id | sudo_request.sale_order_id.user_id
        recipients = self._emails_for_users(users)
        if not recipients:
            self._message_log(body=_('Decision email was not queued because no recipient has an email address.'))
            return
        subject = _('%s %s: %s') % (
            self._request_label, dict(self._fields['state'].selection)[self.state],
            self.invoice_id.display_name)
        body = """
            <html><body style="font-family: Arial, sans-serif; font-size: 14px; color: #000;">
                <p>{label} <strong>{state}</strong>.</p>
                <p><strong>Request:</strong> {request}<br/>
                   <strong>Invoice:</strong> {invoice}<br/>
                   <strong>Sale Order:</strong> {sale_order}<br/>
                   <strong>Decided By:</strong> {decided_by}<br/>
                   <strong>Decision Remarks:</strong> {remarks}</p>
                <p><a href="{url}">View Request in Odoo</a></p>
            </body></html>
        """.format(
            label=escape(self._request_label),
            state=escape(dict(self._fields['state'].selection)[self.state]),
            request=escape(self.name or ''),
            invoice=escape(self.invoice_id.display_name or ''),
            sale_order=escape(self.sale_order_id.display_name or ''),
            decided_by=escape(self.decided_by_id.name or ''),
            remarks=escape(self.decision_remarks or ''),
            url=escape(self._request_url()),
        )
        try:
            with self.env.cr.savepoint():
                message_id = self._send_email_immediate(recipients, subject, body)
                if not message_id:
                    raise UserError(_('The decision email could not be sent.'))
        except Exception as exc:
            _logger.exception('Unable to send decision email for %s', self.name)
            self._message_log(body=_(
                'The decision was completed, but its email could not be sent: %s') % str(exc))

    def _assert_can_decide(self):
        self.ensure_one()
        if self.env.user not in self._approver_users():
            raise UserError(_('You are not allowed to approve or reject this request.'))
        if self.requested_by_id == self.env.user:
            raise UserError(_('You cannot approve or reject your own request.'))

    def _lock_requested(self):
        self.ensure_one()
        self.env.cr.execute(
            'SELECT state FROM %s WHERE id = %%s FOR UPDATE' % self._table,
            (self.id,))
        row = self.env.cr.fetchone()
        self.invalidate_cache(['state'])
        if not row or row[0] != 'requested' or self.state != 'requested':
            raise UserError(_('This request has already been decided.'))

    def _complete_decision(self, state):
        self.ensure_one()
        values = {
            'state': state,
            'decided_by_id': self.env.user.id,
            'decided_date': fields.Datetime.now(),
        }
        self._internal_write(values)
        label = dict(self._fields['state'].selection)[state]
        self._message_log(body=_('%s by %s.') % (label, self.env.user.name))
        self.invoice_id.sudo()._message_log(body=_('%s %s: %s') % (
            self._request_label, label, self.name))

    def action_reject(self):
        for request in self:
            request._assert_can_decide()
            request._lock_requested()
            if not (request.decision_remarks or '').strip():
                raise UserError(_('Decision Remarks are required when rejecting a request.'))
            request._complete_decision('rejected')
            request._send_decision_email()
        return True

    def write(self, vals):
        if self.env.context.get('dex_invoice_request_internal_write') is not _REQUEST_WRITE_SENTINEL:
            allowed = set(vals) <= {'decision_remarks'}
            if not allowed:
                raise UserError(_('Submitted invoice requests cannot be modified.'))
            for request in self:
                if (request.state != 'requested' or
                        self.env.user not in request._approver_users()):
                    raise UserError(_('Only an approver may enter decision remarks.'))
        return super(InvoiceRequestMixin, self).write(vals)

    def unlink(self):
        raise UserError(_('Invoice request history cannot be deleted.'))

    def _internal_write(self, vals):
        return self.sudo().with_context(
            dex_invoice_request_internal_write=_REQUEST_WRITE_SENTINEL).write(vals)


class InvoiceCancelRequest(models.Model):
    _name = 'dex.invoice.cancel.request'
    _description = 'Invoice Cancellation Request'
    _inherit = 'dex.invoice.request.mixin'
    _order = 'requested_date desc, id desc'

    reason_id = fields.Many2one(
        'dex.invoice.cancel.reason', string='Cancellation Reason', required=True,
        readonly=True, ondelete='restrict')
    original_invoice_state = fields.Selection([
        ('draft', 'Draft'),
        ('posted', 'Posted'),
    ], string='Invoice Status When Requested', readonly=True)
    credit_move_id = fields.Many2one(
        'account.move', string='Credit Memo', readonly=True, copy=False)

    _approver_group_xmlid = 'dex_invoice_cancel.group_invoice_cancel_approver'
    _approver_config_parameter = 'dex_invoice_cancel.cancel_request_approver_ids'
    _request_label = 'Invoice Cancellation Request'

    @api.model_create_multi
    def create(self, vals_list):
        sequence = self.env['ir.sequence']
        for vals in vals_list:
            invoice = self.env['account.move'].browse(vals.get('invoice_id')).exists()
            if not invoice:
                raise ValidationError(_('Invoice is required.'))
            invoice.check_access_rights('read')
            invoice.check_access_rule('read')
            self.env.cr.execute('SELECT id FROM account_move WHERE id = %s FOR UPDATE', (invoice.id,))
            invoice.invalidate_cache()
            invoice._dex_cancel_request_eligibility()
            sudo_invoice = invoice.sudo()
            reason = self.env['dex.invoice.cancel.reason'].browse(vals.get('reason_id')).exists()
            if not reason or not reason.active:
                raise ValidationError(_('Select an active cancellation reason.'))
            if not (vals.get('justification') or '').strip():
                raise ValidationError(_('Remarks are required.'))
            vals.update({
                'name': sequence.next_by_code('dex.invoice.cancel.request') or 'New',
                'state': 'requested',
                'requested_by_id': self.env.user.id,
                'requested_date': fields.Datetime.now(),
                'sale_order_id': sudo_invoice.sale_order_id.id,
                'partner_id': sudo_invoice.partner_id.id,
                'currency_id': sudo_invoice.currency_id.id,
                'amount_total': sudo_invoice.amount_total,
                'original_invoice_state': sudo_invoice.state,
                'justification': vals['justification'].strip(),
                'decided_by_id': False,
                'decided_date': False,
                'decision_remarks': False,
                'request_mail_sent': False,
                'credit_move_id': False,
            })
        requests = super(InvoiceCancelRequest, self).create(vals_list)
        for request in requests:
            request._send_request_email()
            request._message_log(body=_('Cancellation request submitted by %s.') % self.env.user.name)
            request.invoice_id._message_log(body=_('Invoice cancellation requested: %s') % request.name)
        return requests

    def _request_email_details(self):
        self.ensure_one()
        return '<p><strong>Cancellation Reason:</strong> %s</p>' % escape(
            self.reason_id.name or '')

    def action_approve(self):
        for request in self:
            request._assert_can_decide()
            request._lock_requested()
            self.env.cr.execute(
                'SELECT id FROM account_move WHERE id = %s FOR UPDATE',
                (request.invoice_id.id,))
            invoice = request.invoice_id.sudo()
            invoice.invalidate_cache()
            invoice._dex_cancel_request_eligibility(request)
            if (request.original_invoice_state and
                    invoice.state != request.original_invoice_state):
                raise UserError(_(
                    'The invoice status changed after this request was submitted.'))
            if invoice.state == 'draft':
                invoice.with_context(
                    dex_invoice_cancel_internal=_CANCEL_APPLY_SENTINEL).button_cancel()
                invoice.with_context(
                    dex_invoice_cancel_internal=_CANCEL_APPLY_SENTINEL).write({
                        'dex_cancelled_by_request': True,
                        'dex_cancel_credit_move_id': False,
                    })
                request._complete_decision('approved')
                request._send_decision_email()
                continue
            approval_date = fields.Date.context_today(request)
            default_values = {
                'date': approval_date,
                'invoice_date': approval_date,
                'ref': _('Cancellation of %s: %s') % (
                    invoice.display_name, request.reason_id.name),
                'source_move_id': invoice.id,
                'invoice_prefix': invoice.invoice_prefix,
                'invoice_number': invoice.invoice_number,
            }
            credit_move = invoice._reverse_moves([default_values], cancel=True)
            if len(credit_move) != 1 or credit_move.state != 'posted':
                raise UserError(_('The cancellation credit memo could not be posted.'))
            request._internal_write({
                'credit_move_id': credit_move.id,
            })
            invoice.with_context(dex_invoice_cancel_internal=_CANCEL_APPLY_SENTINEL).write({
                'dex_cancelled_by_request': True,
                'dex_cancel_credit_move_id': credit_move.id,
            })
            request._complete_decision('approved')
            credit_move.sudo()._message_log(
                body=_('Created by approved cancellation request %s.') % request.name)
            request._send_decision_email()
        return True


class InvoiceUpdateRequest(models.Model):
    _name = 'dex.invoice.update.request'
    _description = 'Invoice Update Request'
    _inherit = 'dex.invoice.request.mixin'
    _order = 'requested_date desc, id desc'

    change_invoice_date = fields.Boolean(string='Change Invoice Date', readonly=True)
    original_invoice_date = fields.Date(string='Original Invoice Date', readonly=True)
    requested_invoice_date = fields.Date(string='Requested Invoice Date', readonly=True)
    line_ids = fields.One2many(
        'dex.invoice.update.request.line', 'request_id', string='Requested Line Changes',
        readonly=True, copy=False)

    _approver_group_xmlid = 'dex_invoice_cancel.group_invoice_update_approver'
    _approver_config_parameter = 'dex_invoice_cancel.update_request_approver_ids'
    _request_label = 'Invoice Update Request'

    @api.model_create_multi
    def create(self, vals_list):
        sequence = self.env['ir.sequence']
        for vals in vals_list:
            invoice = self.env['account.move'].browse(vals.get('invoice_id')).exists()
            if not invoice:
                raise ValidationError(_('Invoice is required.'))
            invoice.check_access_rights('read')
            invoice.check_access_rule('read')
            self.env.cr.execute('SELECT id FROM account_move WHERE id = %s FOR UPDATE', (invoice.id,))
            invoice.invalidate_cache()
            invoice._dex_update_request_eligibility()
            sudo_invoice = invoice.sudo()
            if not (vals.get('justification') or '').strip():
                raise ValidationError(_('Remarks are required.'))
            vals['line_ids'] = self._normalize_line_commands(
                invoice, vals.get('line_ids', []))
            change_invoice_date = bool(vals.get('change_invoice_date'))
            requested_invoice_date = vals.get('requested_invoice_date')
            if sudo_invoice.state == 'draft' and change_invoice_date:
                raise ValidationError(_(
                    'Edit Invoice Date directly while the invoice is draft.'))
            if sudo_invoice.state == 'posted' and vals['line_ids']:
                raise ValidationError(_('Posted invoice lines cannot be updated.'))
            if sudo_invoice.state == 'posted' and not change_invoice_date:
                raise ValidationError(_(
                    'A posted invoice update request must change Invoice Date.'))
            if not change_invoice_date and not vals['line_ids']:
                raise ValidationError(_('Request at least one invoice line change.'))
            if change_invoice_date:
                if not requested_invoice_date:
                    raise ValidationError(_('Requested Invoice Date is required.'))
                if requested_invoice_date == sudo_invoice.invoice_date:
                    raise ValidationError(_(
                        'Requested Invoice Date must differ from the current date.'))
            vals.update({
                'name': sequence.next_by_code('dex.invoice.update.request') or 'New',
                'state': 'requested',
                'requested_by_id': self.env.user.id,
                'requested_date': fields.Datetime.now(),
                'original_invoice_date': sudo_invoice.invoice_date,
                'sale_order_id': sudo_invoice.sale_order_id.id,
                'partner_id': sudo_invoice.partner_id.id,
                'currency_id': sudo_invoice.currency_id.id,
                'amount_total': sudo_invoice.amount_total,
                'justification': vals['justification'].strip(),
                'requested_invoice_date': (
                    vals.get('requested_invoice_date')
                    if vals.get('change_invoice_date') else False),
                'decided_by_id': False,
                'decided_date': False,
                'decision_remarks': False,
                'request_mail_sent': False,
            })
        request_self = self.with_context(
            dex_invoice_request_line_create=_REQUEST_LINE_CREATE_SENTINEL)
        requests = super(InvoiceUpdateRequest, request_self).create(vals_list)
        for request in requests:
            request._validate_payload(check_snapshot=True)
            request._send_request_email()
            request._message_log(body=_('Invoice update request submitted by %s.') % self.env.user.name)
            request.invoice_id._message_log(body=_('Invoice update requested: %s') % request.name)
        return requests

    @api.model
    def _normalize_line_commands(self, invoice, commands):
        normalized = []
        seen_line_ids = set()
        for command in commands:
            if not isinstance(command, (list, tuple)) or len(command) < 3 or command[0] != 0:
                raise ValidationError(_('Update request lines must be newly captured snapshots.'))
            values = dict(command[2] or {})
            line = self.env['account.move.line'].sudo().browse(
                values.get('invoice_line_id')).exists()
            if (not line or line.move_id != invoice.sudo() or line.display_type or
                    line.id in seen_line_ids):
                raise ValidationError(_(
                    'Every requested line must be a unique invoice line on the selected invoice.'))
            seen_line_ids.add(line.id)
            change_type = values.get('change_type')
            if change_type not in ('reduce', 'delete'):
                raise ValidationError(_(
                    'Select Reduce Quantity or Delete Line for every change.'))
            target_quantity = (
                values.get('target_quantity', 0) if change_type == 'reduce' else 0)
            if change_type == 'reduce':
                rounding = line.product_uom_id.rounding or 0.01
                if float_compare(
                        target_quantity, 0,
                        precision_rounding=rounding) <= 0:
                    raise ValidationError(_(
                        'Reduced quantities must remain greater than zero.'))
                if float_compare(
                        target_quantity, line.quantity,
                        precision_rounding=rounding) >= 0:
                    raise ValidationError(_(
                        'A requested quantity must be lower than its original quantity.'))
            normalized.append((0, 0, {
                'invoice_line_id': line.id,
                'product_id': line.product_id.id,
                'description': line.name,
                'product_uom_id': line.product_uom_id.id,
                'original_quantity': line.quantity,
                'original_price_unit': line.price_unit,
                'original_discount': line.discount,
                'tax_ids': [(6, 0, line.tax_ids.ids)],
                'change_type': change_type,
                'target_quantity': target_quantity,
            }))
        return normalized

    def _validate_payload(self, check_snapshot=False):
        self.ensure_one()
        if self.invoice_id.state == 'draft' and self.change_invoice_date:
            raise ValidationError(_(
                'Edit Invoice Date directly while the invoice is draft.'))
        if self.invoice_id.state == 'posted' and self.line_ids:
            raise ValidationError(_('Posted invoice lines cannot be updated.'))
        if self.invoice_id.state == 'posted' and not self.change_invoice_date:
            raise ValidationError(_(
                'A posted invoice update request must change Invoice Date.'))
        if not self.change_invoice_date and not self.line_ids:
            raise ValidationError(_('Request at least one invoice line change.'))
        if self.change_invoice_date:
            if not self.requested_invoice_date:
                raise ValidationError(_('Requested Invoice Date is required.'))
            if self.requested_invoice_date == self.original_invoice_date:
                raise ValidationError(_('Requested Invoice Date must differ from the current date.'))
        for change in self.line_ids:
            line = change.invoice_line_id
            if not line or line.move_id != self.invoice_id or line.display_type:
                raise ValidationError(_('Every requested line must belong to the selected invoice.'))
            if check_snapshot and (
                    line.product_id != change.product_id
                    or line.product_uom_id != change.product_uom_id
                    or line.name != change.description
                    or set(line.tax_ids.ids) != set(change.tax_ids.ids)
                    or float_compare(
                        line.price_unit, change.original_price_unit,
                        precision_rounding=self.currency_id.rounding) != 0
                    or float_compare(
                        line.discount, change.original_discount,
                        precision_digits=6) != 0):
                raise UserError(_(
                    'Invoice line %s changed after this request was submitted.') %
                    change.description)
            rounding = change.product_uom_id.rounding or 0.01
            if check_snapshot and float_compare(
                    line.quantity, change.original_quantity,
                    precision_rounding=rounding) != 0:
                raise UserError(_(
                    'Invoice line %s changed after the request was prepared.') % change.description)
            if change.change_type == 'reduce':
                if float_compare(change.target_quantity, 0, precision_rounding=rounding) <= 0:
                    raise ValidationError(_('Reduced quantities must remain greater than zero.'))
                if float_compare(
                        change.target_quantity, change.original_quantity,
                        precision_rounding=rounding) >= 0:
                    raise ValidationError(_('A requested quantity must be lower than its original quantity.'))
            elif change.change_type != 'delete':
                raise ValidationError(_('Select Reduce Quantity or Delete Line for every change.'))
        return True

    def _request_email_details(self):
        self.ensure_one()
        rows = []
        if self.change_invoice_date:
            rows.append('<li>Invoice Date: %s to %s</li>' % (
                escape(str(self.original_invoice_date or 'Blank')),
                escape(str(self.requested_invoice_date))))
        for change in self.line_ids:
            if change.change_type == 'delete':
                detail = 'Delete line'
            else:
                detail = 'Quantity %s to %s' % (
                    change.original_quantity, change.target_quantity)
            rows.append('<li>%s: %s</li>' % (
                escape(change.description or change.product_id.display_name), escape(detail)))
        return '<p><strong>Requested Changes:</strong></p><ul>%s</ul>' % ''.join(rows)

    def action_approve(self):
        for request in self:
            request._assert_can_decide()
            request._lock_requested()
            self.env.cr.execute(
                'SELECT id FROM account_move WHERE id = %s FOR UPDATE',
                (request.invoice_id.id,))
            invoice = request.invoice_id.sudo()
            invoice.invalidate_cache()
            invoice._dex_update_request_eligibility(request)
            if invoice.invoice_date != request.original_invoice_date:
                raise UserError(_('The invoice date changed after this request was submitted.'))
            request._validate_payload(check_snapshot=True)

            update_context = dict(
                self.env.context,
                dex_invoice_update_request_id=request.id,
                dex_invoice_update_internal=_UPDATE_APPLY_SENTINEL,
                check_move_validity=False,
            )
            if request.change_invoice_date:
                invoice.with_context(update_context).write({
                    'invoice_date': request.requested_invoice_date,
                })
            for change in request.line_ids:
                line = change.invoice_line_id.sudo().with_context(update_context)
                if change.change_type == 'delete':
                    line.unlink()
                else:
                    line.write({'quantity': change.target_quantity})
            if request.line_ids:
                invoice.with_context(update_context)._recompute_dynamic_lines(
                    recompute_all_taxes=True)
            invoice._check_balanced()
            request._complete_decision('approved')
            request._send_decision_email()
        return True


class InvoiceUpdateRequestLine(models.Model):
    _name = 'dex.invoice.update.request.line'
    _description = 'Invoice Update Request Line'
    _order = 'id'

    request_id = fields.Many2one(
        'dex.invoice.update.request', required=True, ondelete='cascade', index=True)
    invoice_line_id = fields.Many2one(
        'account.move.line', string='Invoice Line', readonly=True, ondelete='set null')
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    description = fields.Char(readonly=True)
    product_uom_id = fields.Many2one('uom.uom', string='Unit of Measure', readonly=True)
    original_quantity = fields.Float(
        string='Original Quantity', digits='Product Unit of Measure', readonly=True)
    currency_id = fields.Many2one(
        'res.currency', related='request_id.currency_id', readonly=True)
    original_price_unit = fields.Monetary(
        string='Original Unit Price', currency_field='currency_id', readonly=True)
    original_discount = fields.Float(string='Original Discount', readonly=True)
    tax_ids = fields.Many2many(
        'account.tax', 'dex_invoice_update_request_line_tax_rel',
        'request_line_id', 'tax_id', string='Original Taxes', readonly=True)
    change_type = fields.Selection([
        ('reduce', 'Reduce Quantity'),
        ('delete', 'Delete Line'),
    ], required=True, readonly=True)
    target_quantity = fields.Float(
        string='Requested Quantity', digits='Product Unit of Measure', readonly=True)

    _sql_constraints = [
        ('request_invoice_line_uniq', 'unique(request_id, invoice_line_id)',
         'An invoice line can only appear once in an update request.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(
                'dex_invoice_request_line_create') is not _REQUEST_LINE_CREATE_SENTINEL:
            raise UserError(_('Update request lines can only be captured during submission.'))
        return super(InvoiceUpdateRequestLine, self).create(vals_list)

    def write(self, vals):
        raise UserError(_('Submitted invoice request lines cannot be modified.'))

    def unlink(self):
        raise UserError(_('Submitted invoice request lines cannot be deleted.'))
