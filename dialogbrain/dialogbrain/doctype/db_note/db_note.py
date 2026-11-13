import frappe
from frappe import _
from frappe.model.document import Document


class DBNote(Document):
	def validate(self):
		if not (self.text or "").strip():
			frappe.throw(_("A note cannot be empty"))
