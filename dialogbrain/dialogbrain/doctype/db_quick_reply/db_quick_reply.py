import frappe
from frappe import _
from frappe.model.document import Document

from dialogbrain.permissions import is_manager


class DBQuickReply(Document):
	def validate(self):
		owner = self.owner or frappe.session.user
		if self.shared and not is_manager():
			frappe.throw(_("Only a Chat Manager can share quick replies"), frappe.PermissionError)
		self.shortcut = (self.shortcut or "").strip()
		if self.shortcut:
			clash = frappe.db.exists(
				"DB Quick Reply",
				{"owner": owner, "shortcut": self.shortcut, "name": ("!=", self.name or "")},
			)
			if clash:
				frappe.throw(_("You already have a quick reply with the shortcut {0}").format(self.shortcut))
