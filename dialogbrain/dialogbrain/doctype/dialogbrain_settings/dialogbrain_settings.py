import frappe
from frappe.model.document import Document


class DialogBrainSettings(Document):
	def onload(self):
		from dialogbrain.setup.connect import scheduler_warning
		from dialogbrain.setup.oauth import pop_result

		# shown by the form whenever network_mode is (or is switched to) private
		self.set_onload("scheduler_warning", scheduler_warning("private"))
		# outcome of a "Connect with DialogBrain" (OAuth) round trip, shown once
		self.set_onload("oauth_result", pop_result(frappe.session.user))

	def validate(self):
		if self.flags.dialogbrain_server_write:
			return
		from dialogbrain.setup.connect import SERVER_FIELDS, configure_webhook

		# R14: a form save never overwrites what the sync/connect code manages.
		stored = frappe.db.get_value(
			self.doctype, self.name, [*SERVER_FIELDS, "network_mode", "public_site_url", "allow_agent_writes"], as_dict=True
		) or frappe._dict()
		for field in SERVER_FIELDS:
			self.set(field, stored.get(field))
		if frappe.utils.cint(stored.connected):
			self._refuse_key_or_address_change()

		mode = self.network_mode or "public"
		url_changed = (stored.public_site_url or "") != (self.public_site_url or "")
		if self.connected and (
			(stored.network_mode or "public") != mode or (mode == "public" and url_changed)
		):
			configure_webhook(self, mode)

		# A plain form save of the writes toggle re-applies DialogBrain's denied_tools too
		# (the form's own toggle uses agent_integration.set_agent_writes).
		if self.connected and frappe.utils.cint(stored.allow_agent_writes) != frappe.utils.cint(self.allow_agent_writes):
			from dialogbrain.client import DialogBrainError
			from dialogbrain.setup.agent_integration import apply_denied_tools

			try:
				apply_denied_tools(self)
			except (DialogBrainError, frappe.ValidationError) as exc:
				frappe.msgprint(
					frappe._("DialogBrain agents were not updated ({0}). Press Reconnect to retry.").format(
						str(exc)[:300]
					),
					indicator="orange",
				)

	def _refuse_key_or_address_change(self):
		"""Residual fix R2: while connected, a form save may not store another API key or
		base URL (it would point sync and sends at another workspace before Reconnect's
		workspace check). Server writes (``_server_save``, OAuth) never get here."""
		stored_url = (frappe.db.get_single_value(self.doctype, "base_url") or "").strip().rstrip("/")
		url_changed = stored_url != (self.base_url or "").strip().rstrip("/")
		new_key = self.api_key or ""
		key_changed = False
		if not self.is_dummy_password(new_key):  # the form sends a masked value when unchanged
			from frappe.utils.password import get_decrypted_password

			stored_key = get_decrypted_password(self.doctype, self.name, "api_key", raise_exception=False) or ""
			key_changed = new_key != stored_key
		if url_changed or key_changed:
			frappe.throw(frappe._("Disconnect first to change the DialogBrain key or address"))
