import frappe

ROLES = ("Chat Agent", "Chat Manager")
# The DialogBrain AI agent's API user (Plan 3). desk_access=1 keeps that user a System
# User: with no desk-access role Frappe demotes it to Website User (B1 amendment).
AGENT_ROLE = "DialogBrain Agent API"

# Documents the AI agent creates point back at the chat they came from (spec §7.4).
DB_CONVERSATION_DOCTYPES = ("Sales Order", "Quotation", "Lead")


def create_roles():
	"""Idempotently create the DialogBrain roles."""
	for role in (*ROLES, AGENT_ROLE):
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)


def create_custom_fields():
	"""Idempotently add the read-only ``db_conversation`` link to the agent's documents."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields as _create

	field = {
		"fieldname": "db_conversation",
		"label": "DialogBrain Conversation",
		"fieldtype": "Link",
		"options": "DB Conversation",
		"read_only": 1,
		"no_copy": 1,
		"print_hide": 1,
		"report_hide": 1,
	}
	_create({doctype: [dict(field)] for doctype in DB_CONVERSATION_DOCTYPES}, update=True)


def after_install():
	create_roles()
	create_custom_fields()
