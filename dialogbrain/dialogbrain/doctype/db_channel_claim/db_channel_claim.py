import frappe
from frappe.model.document import Document


class DBChannelClaim(Document):
	"""A pending "connect my number" request: who started it and who will own the channel.

	No phone number, code or password is ever stored here."""


def on_doctype_update():
	frappe.db.add_index("DB Channel Claim", ["status", "channel_type"])
