# -*- coding: utf-8 -*-

from ast import literal_eval

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    dex_cancel_request_approver_ids = fields.Many2many(
        'res.users', 'dex_cancel_settings_approver_rel',
        'settings_id', 'user_id', string='Cancel Request Approvers')
    dex_update_request_approver_ids = fields.Many2many(
        'res.users', 'dex_update_settings_approver_rel',
        'settings_id', 'user_id', string='Update Request Approvers')
    dex_invoice_request_email_from = fields.Char(
        string='Request Email From',
        config_parameter='dex_invoice_cancel.request_email_from')

    @api.model
    def _dex_approver_ids(self, parameter, group_xmlid):
        value = self.env['ir.config_parameter'].sudo().get_param(parameter)
        if value:
            try:
                ids = literal_eval(value)
                if isinstance(ids, (list, tuple)):
                    return [int(user_id) for user_id in ids]
            except (SyntaxError, ValueError, TypeError):
                pass
        return self.env.ref(group_xmlid).sudo().users.ids

    @api.model
    def get_values(self):
        values = super(ResConfigSettings, self).get_values()
        values.update({
            'dex_cancel_request_approver_ids': [(6, 0, self._dex_approver_ids(
                'dex_invoice_cancel.cancel_request_approver_ids',
                'dex_invoice_cancel.group_invoice_cancel_approver'))],
            'dex_update_request_approver_ids': [(6, 0, self._dex_approver_ids(
                'dex_invoice_cancel.update_request_approver_ids',
                'dex_invoice_cancel.group_invoice_update_approver'))],
        })
        return values

    @api.model
    def _dex_validate_approvers(self, users, label):
        users = users.filtered(lambda user: user.active and not user.share)
        recipients = users.filtered(lambda user: user.email or user.partner_id.email)
        if not recipients:
            raise UserError(_(
                'Configure at least one active %s with a valid email address.') % label)

    def set_values(self):
        self.ensure_one()
        self._dex_validate_approvers(
            self.dex_cancel_request_approver_ids, _('Cancel Request Approver'))
        self._dex_validate_approvers(
            self.dex_update_request_approver_ids, _('Update Request Approver'))
        result = super(ResConfigSettings, self).set_values()
        parameters = self.env['ir.config_parameter'].sudo()
        configurations = (
            (
                self.dex_cancel_request_approver_ids,
                'dex_invoice_cancel.cancel_request_approver_ids',
                'dex_invoice_cancel.group_invoice_cancel_approver',
            ),
            (
                self.dex_update_request_approver_ids,
                'dex_invoice_cancel.update_request_approver_ids',
                'dex_invoice_cancel.group_invoice_update_approver',
            ),
        )
        for users, parameter, group_xmlid in configurations:
            user_ids = users.filtered(lambda user: user.active and not user.share).ids
            parameters.set_param(parameter, repr(user_ids))
            self.env.ref(group_xmlid).sudo().write({'users': [(6, 0, user_ids)]})
        return result
