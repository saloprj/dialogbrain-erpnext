import frappe
from frappe.model.document import Document


class DBMessage(Document):
	def validate(self):
		# Unique indexes allow many NULLs but only one "": normalise blanks.
		for fieldname in ("db_id", "client_ref"):
			if not self.get(fieldname):
				self.set(fieldname, None)
		# delivery_status describes our replies only: inbound rows keep it empty (NULL).
		if self.direction == "in" and self.delivery_status != "deleted":
			self.delivery_status = None
		elif not self.delivery_status:
			self.delivery_status = None
		# A provisional row (partial event, direction unknown) must not get the Select
		# defaults ("in" / "customer" / "pending"): see dialogbrain.sync.upsert.
		if self.flags.dialogbrain_provisional:
			for fieldname in ("direction", "author_type", "delivery_status"):
				self.set(fieldname, None)


def on_doctype_update():
	frappe.db.add_index("DB Message", ["conversation", "sent_at"], "conversation_sent_at_index")
	# the scheduler's stale-pending sweep (dialogbrain.sync.sender.requeue_stale_replies)
	frappe.db.add_index("DB Message", ["delivery_status", "modified"], "delivery_status_modified_index")
