"""Channel-based access control for DB Conversation / DB Message / DB Draft.

* System Manager (incl. Administrator) and Chat Manager see everything.
* Chat Agent sees conversations of channels whose ``allowed_users`` is empty or
  contains the user.
* Anyone else sees nothing.

Enforced via ``permission_query_conditions`` (lists) and ``has_permission`` (docs).
"""

import frappe
from frappe import _

SEE_ALL_ROLES = ("System Manager", "Chat Manager")


def allowed_channels(user):
	"""Channel names the user may access, or None meaning all channels."""
	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	if roles & set(SEE_ALL_ROLES):
		return None
	if "Chat Agent" not in roles:
		return []
	return frappe.db.sql_list(
		"""SELECT ch.name FROM `tabDB Channel` ch
		WHERE NOT EXISTS (SELECT 1 FROM `tabDB Channel User` cu
			WHERE cu.parent = ch.name AND cu.parenttype = 'DB Channel')
		OR EXISTS (SELECT 1 FROM `tabDB Channel User` cu
			WHERE cu.parent = ch.name AND cu.parenttype = 'DB Channel' AND cu.user = %s)""",
		(user,),
	)


def permitted_users(channel):
	"""Enabled users who may access ``channel``: Chat Managers plus allowed Chat Agents.

	Only explicit Has Role rows count, so Administrator/Guest are listed only if they
	were given the role explicitly.
	"""
	managers = _enabled_with_role("Chat Manager")
	agents = _enabled_with_role("Chat Agent")
	allowed = frappe.db.sql_list(
		"SELECT user FROM `tabDB Channel User` WHERE parent = %s AND parenttype = 'DB Channel'",
		(channel,),
	)
	if allowed:
		agents = [u for u in agents if u in set(allowed)]
	return sorted(set(managers) | set(agents))


def _enabled_with_role(role):
	return frappe.db.sql_list(
		"""SELECT u.name FROM `tabUser` u
		JOIN `tabHas Role` hr ON hr.parent = u.name AND hr.parenttype = 'User'
		WHERE u.enabled = 1 AND hr.role = %s""",
		(role,),
	)


def _channel_in(column, user):
	channels = allowed_channels(user)
	if channels is None:
		return ""
	if not channels:
		return "1=0"
	return f"{column} in ({', '.join(frappe.db.escape(c) for c in channels)})"


def conversation_query(user=None):
	return _channel_in("`tabDB Conversation`.`channel`", user)


def _via_conversation(doctype, user):
	channels = allowed_channels(user)
	if channels is None:
		return ""
	if not channels:
		return "1=0"
	in_list = ", ".join(frappe.db.escape(c) for c in channels)
	return (
		f"`tab{doctype}`.`conversation` in (SELECT `name` FROM `tabDB Conversation` "
		f"WHERE `channel` in ({in_list}))"
	)


def message_query(user=None):
	return _via_conversation("DB Message", user)


def draft_query(user=None):
	return _via_conversation("DB Draft", user)


def _can_see_channel(channel, user):
	channels = allowed_channels(user)
	return channels is None or (bool(channel) and channel in channels)


def _doc_get(doc, field):
	if isinstance(doc, str):
		return None
	return doc.get(field) if hasattr(doc, "get") else getattr(doc, field, None)


def conversation_has_permission(doc, ptype=None, user=None, **kwargs):
	user = user or frappe.session.user
	if doc is None:
		return True
	if isinstance(doc, str):
		channel = frappe.db.get_value("DB Conversation", doc, "channel")
	else:
		channel = _doc_get(doc, "channel")
	return _can_see_channel(channel, user)


def _child_has_permission(doc, user):
	user = user or frappe.session.user
	if doc is None:
		return True
	conversation = None if isinstance(doc, str) else _doc_get(doc, "conversation")
	if not conversation:
		return allowed_channels(user) is None
	channel = frappe.db.get_value("DB Conversation", conversation, "channel")
	return _can_see_channel(channel, user)


def message_has_permission(doc, ptype=None, user=None, **kwargs):
	return _child_has_permission(doc, user)


def draft_has_permission(doc, ptype=None, user=None, **kwargs):
	return _child_has_permission(doc, user)


def assert_can_access(conversation, user=None):
	user = user or frappe.session.user
	channel = frappe.db.get_value("DB Conversation", conversation, "channel")
	if not frappe.db.exists("DB Conversation", conversation) or not _can_see_channel(channel, user):
		frappe.throw(_("You do not have access to this conversation"), frappe.PermissionError)
