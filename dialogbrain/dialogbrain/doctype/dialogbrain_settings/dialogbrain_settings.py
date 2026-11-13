import frappe
from frappe.model.document import Document


class DialogBrainSettings(Document):
	def validate(self):
		if self.flags.dialogbrain_server_write:
			return
		from dialogbrain.setup.connect import SERVER_FIELDS, configure_webhook

		# R14: a form save never overwrites what the sync/connect code manages.
		stored = frappe.db.get_value(
			self.doctype, self.name, [*SERVER_FIELDS, "network_mode", "public_site_url"], as_dict=True
		) or frappe._dict()
		for field in SERVER_FIELDS:
			self.set(field, stored.get(field))

		mode = self.network_mode or "public"
		url_changed = (stored.public_site_url or "") != (self.public_site_url or "")
		if self.connected and (
			(stored.network_mode or "public") != mode or (mode == "public" and url_changed)
		):
			configure_webhook(self, mode)
