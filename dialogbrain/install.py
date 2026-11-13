import frappe

ROLES = ("Chat Agent", "Chat Manager")


def create_roles():
	"""Idempotently create the DialogBrain roles."""
	for role in ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)


def after_install():
	create_roles()
