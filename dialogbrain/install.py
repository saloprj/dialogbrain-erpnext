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
		# owned by the app's module, so Frappe's uninstall removes them too (final wave F4)
		"module": "DialogBrain",
	}
	_create({doctype: [dict(field)] for doctype in DB_CONVERSATION_DOCTYPES}, update=True)


def after_install():
	create_roles()
	create_custom_fields()


def before_uninstall():
	"""Leave no agent access behind (final wave F4). Never fails the uninstall: each step
	is best effort and logged.

	1. If connected, switch the ERPNext integration off for every DialogBrain agent.
	2. Disable the agent user and clear its API key and secret.
	3. Delete the ``db_conversation`` Custom Fields (their DocType is about to go)."""
	for step in (_switch_agents_off, _disable_agent_user, delete_custom_fields):
		try:
			step()
		except Exception:  # noqa: BLE001 -- the uninstall must go through
			frappe.log_error(title=f"DialogBrain uninstall: {step.__name__} failed")


def _switch_agents_off():
	from dialogbrain.setup import agent_integration

	doc = frappe.get_single("DialogBrain Settings")
	if not (doc.connected and doc.integration_id) or (doc.network_mode or "public") == "private":
		return
	client = agent_integration._client(doc)
	agent_integration._attach_agents(
		client, int(doc.integration_id), agent_integration.denied_tools(doc), enabled=False
	)


def _disable_agent_user():
	from frappe.utils.password import remove_encrypted_password

	from dialogbrain.agent_api import agent_user_name

	user = frappe.db.get_single_value("DialogBrain Settings", "erpnext_agent_user") or agent_user_name()
	if not frappe.db.exists("User", user):
		return
	frappe.db.set_value("User", user, {"enabled": 0, "api_key": None})
	remove_encrypted_password("User", user, "api_secret")
	frappe.clear_document_cache("User", user)


def delete_custom_fields():
	for doctype in DB_CONVERSATION_DOCTYPES:
		for name in frappe.get_all(
			"Custom Field", filters={"dt": doctype, "fieldname": "db_conversation"}, pluck="name"
		):
			frappe.delete_doc("Custom Field", name, ignore_permissions=True, force=True)
