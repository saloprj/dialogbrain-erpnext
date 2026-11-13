import frappe
from frappe.model.document import Document


class DBMessage(Document):
	def validate(self):
		# Unique indexes allow many NULLs but only one "": normalise blanks.
		for fieldname in ("db_id", "client_ref"):
			if not self.get(fieldname):
				self.set(fieldname, None)


def on_doctype_update():
	frappe.db.add_index("DB Message", ["conversation", "sent_at"], "conversation_sent_at_index")
