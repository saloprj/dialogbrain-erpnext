import frappe
from frappe.model.document import Document


class DBConversation(Document):
	def validate(self):
		# Unique indexes allow many NULLs but only one "": normalise blanks.
		for fieldname in ("thread_id", "thread_ref"):
			if not (self.get(fieldname) or "").strip():
				self.set(fieldname, None)


def on_doctype_update():
	frappe.db.add_index("DB Conversation", ["status", "last_message_at"], "status_last_message_at_index")
	frappe.db.add_index("DB Conversation", ["assigned_to", "status"], "assigned_to_status_index")
	frappe.db.add_index("DB Conversation", ["phone"], "phone_index")
	frappe.db.add_index("DB Conversation", ["email"], "email_index")
	# the customer card and party lookups filter by the linked party
	frappe.db.add_index("DB Conversation", ["party_type", "party"], "party_type_party_index")
	# a channel ref identifies one thread per channel account (see dialogbrain.sync.upsert)
	frappe.db.add_unique("DB Conversation", ["channel", "thread_ref"], "channel_thread_ref_unique")
