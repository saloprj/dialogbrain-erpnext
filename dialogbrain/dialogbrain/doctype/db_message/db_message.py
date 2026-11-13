import frappe
from frappe.model.document import Document


class DBMessage(Document):
	def validate(self):
		# Unique indexes allow many NULLs but only one "": normalise blanks.
		for fieldname in ("db_id", "client_ref"):
			if not self.get(fieldname):
				self.set(fieldname, None)
		# A provisional row (partial event, direction unknown) must not get the Select
		# defaults ("in" / "customer" / "pending"): see dialogbrain.sync.upsert.
		if self.flags.dialogbrain_provisional:
			for fieldname in ("direction", "author_type", "delivery_status"):
				self.set(fieldname, None)


def on_doctype_update():
	frappe.db.add_index("DB Message", ["conversation", "sent_at"], "conversation_sent_at_index")
