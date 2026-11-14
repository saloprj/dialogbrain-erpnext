import frappe
from frappe import _
from frappe.model.document import Document

# Which DialogBrain thread the row mirrors and which ERPNext party it is linked to. The
# link decides what the AI agent may release to the chat (R12/R13), so only the app's own
# server code (``doc.flags.dialogbrain_server_write``; flags never come from a request:
# Frappe drops ``flags`` in Document.set/update and the REST/``frappe.client`` handlers) or
# a System Manager may write them through the document API. Chat Managers link a party
# with ``dialogbrain.party.set_party``, which checks read access to that party.
PROTECTED_FIELDS = (
	"thread_id",
	"thread_ref",
	"channel",
	"dialogbrain_instance",
	"db_contact_id",
	"phone",
	"email",
	"party_type",
	"party",
	"contact",
	"link_source",
)


def _may_write_identity() -> bool:
	return frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles()


class DBConversation(Document):
	def validate(self):
		# Unique indexes allow many NULLs but only one "": normalise blanks.
		for fieldname in ("thread_id", "thread_ref"):
			if not (self.get(fieldname) or "").strip():
				self.set(fieldname, None)
		self._guard_identity()

	def _guard_identity(self):
		if self.flags.dialogbrain_server_write or _may_write_identity():
			return
		before = None if self.is_new() else self.get_doc_before_save()
		changed = [
			f
			for f in PROTECTED_FIELDS
			if str(self.get(f) or "") != str((before.get(f) if before else None) or "")
		]
		if changed:
			frappe.throw(
				_("Only DialogBrain sync or a System Manager can change these fields: {0}").format(
					", ".join(_(self.meta.get_label(f)) for f in changed)
				),
				frappe.PermissionError,
			)


def on_doctype_update():
	frappe.db.add_index("DB Conversation", ["status", "last_message_at"], "status_last_message_at_index")
	frappe.db.add_index("DB Conversation", ["assigned_to", "status"], "assigned_to_status_index")
	frappe.db.add_index("DB Conversation", ["phone"], "phone_index")
	frappe.db.add_index("DB Conversation", ["email"], "email_index")
	# the customer card and party lookups filter by the linked party
	frappe.db.add_index("DB Conversation", ["party_type", "party"], "party_type_party_index")
	# a channel ref identifies one thread per channel account (see dialogbrain.sync.upsert)
	frappe.db.add_unique("DB Conversation", ["channel", "thread_ref"], "channel_thread_ref_unique")
