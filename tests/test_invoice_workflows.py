# -*- coding: utf-8 -*-

from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.dex_mail.models.mail import Mail


@tagged('post_install', '-at_install')
class TestInvoiceApprovalWorkflows(TransactionCase):

    def setUp(self):
        super(TestInvoiceApprovalWorkflows, self).setUp()
        self.company = self.env.user.company_id
        self.partner = self.env['res.partner'].create({
            'name': 'DEX TEST Invoice Approval Customer',
            'email': 'customer.invoice.approval@example.com',
            'customer_rank': 1,
        })
        self.requester = self._create_user(
            'dex_invoice_requester', 'requester.invoice.approval@example.com', [
                'dex_invoice_cancel.group_invoice_cancel_requester',
                'dex_invoice_cancel.group_invoice_update_requester',
            ])
        self.approver = self._create_user(
            'dex_invoice_approver', 'approver.invoice.approval@example.com', [
                'dex_invoice_cancel.group_invoice_cancel_approver',
                'dex_invoice_cancel.group_invoice_update_approver',
            ])
        self.env.ref('dex_invoice_cancel.group_invoice_cancel_approver').write({
            'users': [(6, 0, [self.approver.id])],
        })
        self.env.ref('dex_invoice_cancel.group_invoice_update_approver').write({
            'users': [(6, 0, [self.approver.id])],
        })
        self.reason = self.env['dex.invoice.cancel.reason'].sudo().create({
            'name': 'DEX TEST Wrong customer request',
        })
        self.pricelist = self.env['product.pricelist'].create({
            'name': 'DEX TEST Invoice Approval Pricelist',
            'currency_id': self.company.currency_id.id,
        })
        self.partner.property_product_pricelist = self.pricelist
        self.product = self.env['product.product'].create({
            'name': 'DEX TEST Invoice Approval Service',
            'type': 'service',
            'invoice_policy': 'order',
            'list_price': 125.0,
        })

    def _create_user(self, login, email, group_xmlids):
        groups = [
            self.env.ref('base.group_user').id,
            self.env.ref('account.group_account_manager').id,
            self.env.ref('sales_team.group_sale_salesman').id,
        ]
        groups.extend(self.env.ref(xmlid).id for xmlid in group_xmlids)
        return self.env['res.users'].with_context(no_reset_password=True).create({
            'name': login,
            'login': login,
            'email': email,
            'company_id': self.company.id,
            'company_ids': [(6, 0, [self.company.id])],
            'groups_id': [(6, 0, groups)],
        })

    def _create_sale_invoice(self, quantity=5.0):
        order = self.env['sale.order'].create({
            'partner_id': self.partner.id,
            'user_id': self.requester.id,
            'pricelist_id': self.pricelist.id,
        })
        sale_line = self.env['sale.order.line'].create({
            'order_id': order.id,
            'name': self.product.display_name,
            'product_id': self.product.id,
            'product_uom_qty': quantity,
            'product_uom': self.product.uom_id.id,
            'price_unit': self.product.list_price,
            'tax_id': [(6, 0, [])],
        })
        account = self.product.property_account_income_id or \
            self.product.categ_id.property_account_income_categ_id
        if not account:
            account = self.env['account.account'].search([
                ('company_id', '=', self.company.id),
                ('user_type_id.type', '=', 'other'),
                ('deprecated', '=', False),
            ], limit=1)
        invoice = self.env['account.move'].with_context(default_type='out_invoice').create({
            'type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_date': fields.Date.today(),
            'invoice_line_ids': [(0, 0, {
                'name': self.product.display_name,
                'product_id': self.product.id,
                'product_uom_id': self.product.uom_id.id,
                'quantity': quantity,
                'price_unit': self.product.list_price,
                'account_id': account.id,
                'sale_line_ids': [(6, 0, [sale_line.id])],
            })],
        })
        self.assertEqual(invoice.sale_order_id, order)
        return order, sale_line, invoice

    def _create_cancel_request(self, invoice):
        with patch.object(Mail, 'send_external', autospec=True) as send_mail:
            request = self.env['dex.invoice.cancel.request'].with_user(
                self.requester).create({
                    'invoice_id': invoice.id,
                    'reason_id': self.reason.id,
                    'justification': 'DEX TEST approved cancellation case',
                })
        self.assertTrue(send_mail.called)
        return request

    def _create_update_request(self, invoice, target_quantity=3.0,
                               delete=False, change_date=False):
        line = invoice.invoice_line_ids.filtered(lambda item: not item.display_type)[:1]
        values = {
            'invoice_id': invoice.id,
            'justification': 'DEX TEST approved update case',
            'change_invoice_date': change_date,
            'requested_invoice_date': (
                fields.Date.today() + timedelta(days=1) if change_date else False),
            'line_ids': [(0, 0, {
                'invoice_line_id': line.id,
                'product_id': line.product_id.id,
                'description': line.name,
                'product_uom_id': line.product_uom_id.id,
                'original_quantity': line.quantity,
                'change_type': 'delete' if delete else 'reduce',
                'target_quantity': 0 if delete else target_quantity,
            })],
        }
        with patch.object(Mail, 'send_external', autospec=True):
            return self.env['dex.invoice.update.request'].with_user(
                self.requester).create(values)

    def test_cancel_approval_posts_and_reconciles_full_credit(self):
        order, sale_line, invoice = self._create_sale_invoice()
        invoice.post()
        request = self._create_cancel_request(invoice)

        with patch.object(Mail, 'send_external', autospec=True):
            request.with_user(self.approver).action_approve()

        invoice.invalidate_cache()
        request.invalidate_cache()
        credit = request.credit_move_id
        self.assertEqual(request.state, 'approved')
        self.assertEqual(invoice.state, 'posted')
        self.assertTrue(invoice.dex_cancelled_by_request)
        self.assertEqual(invoice.dex_cancel_credit_move_id, credit)
        self.assertEqual(credit.state, 'posted')
        self.assertEqual(credit.type, 'out_refund')
        self.assertEqual(credit.reversed_entry_id, invoice)
        self.assertEqual(credit.source_move_id, invoice)
        self.assertEqual(credit.invoice_date, fields.Date.today())
        self.assertTrue(invoice.currency_id.is_zero(invoice.amount_residual))
        self.assertEqual(credit.invoice_line_ids.mapped('sale_line_ids'), sale_line)
        sale_line.invalidate_cache(['qty_invoiced', 'qty_to_invoice'])
        self.assertEqual(sale_line.qty_invoiced, 0)

    def test_cancel_eligibility_and_duplicate_request_guards(self):
        _order, _sale_line, invoice = self._create_sale_invoice()
        with self.assertRaises(UserError):
            invoice._dex_cancel_request_eligibility()
        invoice.post()
        self._create_cancel_request(invoice)
        with patch.object(Mail, 'send_external', autospec=True), self.assertRaises(UserError):
            self.env['dex.invoice.cancel.request'].with_user(self.requester).create({
                'invoice_id': invoice.id,
                'reason_id': self.reason.id,
                'justification': 'DEX TEST duplicate',
            })

    def test_existing_credit_activity_blocks_cancellation(self):
        _order, _sale_line, invoice = self._create_sale_invoice()
        invoice.post()
        self.env['account.move'].with_context(default_type='out_refund').create({
            'type': 'out_refund',
            'partner_id': self.partner.id,
            'source_move_id': invoice.id,
        })
        with self.assertRaises(UserError):
            invoice._dex_cancel_request_eligibility()

    def test_self_approval_and_repeat_decision_are_rejected(self):
        _order, _sale_line, invoice = self._create_sale_invoice()
        invoice.post()
        request = self._create_cancel_request(invoice)
        self.requester.write({'groups_id': [(4, self.env.ref(
            'dex_invoice_cancel.group_invoice_cancel_approver').id)]})
        with self.assertRaises(UserError):
            request.with_user(self.requester).action_approve()
        request.with_user(self.approver).write({'decision_remarks': 'DEX TEST reject'})
        with patch.object(Mail, 'send_external', autospec=True):
            request.with_user(self.approver).action_reject()
        with self.assertRaises(UserError):
            request.with_user(self.approver).action_reject()

    def test_request_history_and_audit_fields_are_immutable(self):
        _order, _sale_line, invoice = self._create_sale_invoice()
        invoice.post()
        request = self._create_cancel_request(invoice)
        with self.assertRaises(UserError):
            request.sudo().write({'justification': 'tampered'})
        with self.assertRaises(UserError):
            request.sudo().unlink()
        with self.assertRaises(UserError):
            invoice.sudo().write({'dex_cancelled_by_request': True})

    def test_update_approval_reduces_quantity_and_changes_date(self):
        _order, sale_line, invoice = self._create_sale_invoice()
        requested_date = fields.Date.today() + timedelta(days=1)
        request = self._create_update_request(
            invoice, target_quantity=3.0, change_date=True)

        with patch.object(Mail, 'send_external', autospec=True):
            request.with_user(self.approver).action_approve()

        invoice.invalidate_cache()
        sale_line.invalidate_cache(['qty_invoiced', 'qty_to_invoice'])
        self.assertEqual(request.state, 'approved')
        self.assertEqual(invoice.invoice_date, requested_date)
        self.assertEqual(invoice.invoice_line_ids.filtered(
            lambda line: not line.display_type).quantity, 3.0)
        self.assertEqual(sale_line.qty_invoiced, 3.0)

    def test_update_deletes_only_requested_line(self):
        _order, _sale_line, invoice = self._create_sale_invoice()
        target = invoice.invoice_line_ids.filtered(lambda line: not line.display_type)[:1]
        request = self._create_update_request(invoice, delete=True)
        with patch.object(Mail, 'send_external', autospec=True):
            request.with_user(self.approver).action_approve()
        self.assertFalse(target.exists())

    def test_direct_edits_forged_context_and_pending_post_are_blocked(self):
        _order, _sale_line, invoice = self._create_sale_invoice()
        line = invoice.invoice_line_ids.filtered(lambda item: not item.display_type)[:1]
        with self.assertRaises(UserError):
            invoice.write({'invoice_date': fields.Date.today() + timedelta(days=2)})
        with self.assertRaises(UserError):
            line.write({'quantity': 4})
        request = self._create_update_request(invoice)
        forged = dict(
            dex_invoice_update_request_id=request.id,
            dex_invoice_update_internal=True,
        )
        with self.assertRaises(UserError):
            line.with_context(forged).write({'quantity': 3})
        with self.assertRaises(UserError):
            invoice.action_post()

    def test_update_rejects_increase_and_stale_snapshot(self):
        _order, _sale_line, invoice = self._create_sale_invoice()
        line = invoice.invoice_line_ids.filtered(lambda item: not item.display_type)[:1]
        with patch.object(Mail, 'send_external', autospec=True), self.assertRaises(ValidationError):
            self.env['dex.invoice.update.request'].with_user(self.requester).create({
                'invoice_id': invoice.id,
                'justification': 'DEX TEST invalid increase',
                'line_ids': [(0, 0, {
                    'invoice_line_id': line.id,
                    'product_id': line.product_id.id,
                    'description': line.name,
                    'product_uom_id': line.product_uom_id.id,
                    'original_quantity': line.quantity,
                    'change_type': 'reduce',
                    'target_quantity': line.quantity + 1,
                })],
            })
        request = self._create_update_request(invoice)
        self.env.cr.execute(
            'UPDATE account_move_line SET quantity = %s WHERE id = %s',
            (4.0, line.id))
        line.invalidate_cache()
        with self.assertRaises(UserError):
            request.with_user(self.approver).action_approve()

    def test_update_rejects_stale_pricing_snapshot(self):
        _order, _sale_line, invoice = self._create_sale_invoice()
        line = invoice.invoice_line_ids.filtered(lambda item: not item.display_type)[:1]
        request = self._create_update_request(invoice)
        self.env.cr.execute(
            'UPDATE account_move_line SET price_unit = %s WHERE id = %s',
            (line.price_unit + 1, line.id))
        line.invalidate_cache()
        with self.assertRaises(UserError):
            request.with_user(self.approver).action_approve()

    def test_missing_approver_email_blocks_submission(self):
        _order, _sale_line, invoice = self._create_sale_invoice()
        invoice.post()
        self.approver.write({'email': False})
        self.approver.partner_id.write({'email': False})
        with self.assertRaises(UserError):
            self.env['dex.invoice.cancel.request'].with_user(self.requester).create({
                'invoice_id': invoice.id,
                'reason_id': self.reason.id,
                'justification': 'DEX TEST missing recipient',
            })

    def test_decision_email_failure_does_not_undo_update(self):
        _order, _sale_line, invoice = self._create_sale_invoice()
        request = self._create_update_request(invoice)
        with patch.object(Mail, 'send_external', autospec=True,
                          side_effect=RuntimeError('DEX TEST mail failure')):
            request.with_user(self.approver).action_approve()
        self.assertEqual(request.state, 'approved')
        self.assertEqual(invoice.invoice_line_ids.filtered(
            lambda line: not line.display_type).quantity, 3.0)
