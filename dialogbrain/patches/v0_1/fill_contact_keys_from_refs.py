"""Fill phone / email of existing conversations from their channel ref and link parties
(fix wave F2).

Before the fix only webhook message metadata supplied them, so backfilled conversations
(almost all) had none and party auto-linking never ran. Only empty fields are filled; a
manual party link is never touched (``link_party`` skips it). Idempotent.
"""

import frappe

from dialogbrain.party import contact_keys_from_ref, link_party


def execute():
	rows = frappe.db.sql(
		"""select name, thread_ref, phone, email from `tabDB Conversation`
		where thread_ref like 'whatsapp:%%' or thread_ref like 'viber:%%' or thread_ref like 'email:dm:%%'""",
		as_dict=True,
	)
	for row in rows:
		updates = {
			field: value
			for field, value in contact_keys_from_ref(row.thread_ref).items()
			if not row.get(field) and len(value) <= 140
		}
		if not updates:
			continue
		frappe.db.set_value("DB Conversation", row.name, updates, update_modified=False)
		link_party(row.name)
