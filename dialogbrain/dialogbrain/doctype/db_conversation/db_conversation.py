import frappe
from frappe.model.document import Document


class DBConversation(Document):
	pass


def on_doctype_update():
	frappe.db.add_index("DB Conversation", ["status", "last_message_at"], "status_last_message_at_index")
	frappe.db.add_index("DB Conversation", ["assigned_to", "status"], "assigned_to_status_index")
	frappe.db.add_index("DB Conversation", ["phone"], "phone_index")
	frappe.db.add_index("DB Conversation", ["email"], "email_index")
