"""Realtime events and Notification Log alerts for DialogBrain conversations."""

import frappe
from frappe import _
from frappe.utils import escape_html

from dialogbrain.permissions import permitted_users

REALTIME_PREFIX = "dialogbrain:"
BROADCAST_REASONS = ("first_inbound", "handoff", "draft_order")


def publish(conversation: str, kind: str, payload: dict) -> None:
	"""Send ``dialogbrain:<kind>`` to :func:`realtime_users` of the conversation's channel."""
	channel = frappe.db.get_value("DB Conversation", conversation, "channel")
	if not channel:
		return
	payload = {"conversation": conversation, **(payload or {})}
	for user in realtime_users(channel):
		frappe.publish_realtime(REALTIME_PREFIX + kind, payload, user=user, after_commit=True)


def realtime_users(channel: str) -> list:
	"""Realtime recipients (Ruling R9): the channel's permitted users plus every enabled user
	with an explicit System Manager role, who can see every conversation anyway. Notification
	Log recipients (:func:`recipients`) stay on :func:`permitted_users`."""
	system_managers = frappe.db.sql_list(
		"""SELECT u.name FROM `tabUser` u
		JOIN `tabHas Role` hr ON hr.parent = u.name AND hr.parenttype = 'User'
		WHERE u.enabled = 1 AND hr.role = 'System Manager' AND u.name != 'Guest'"""
	)
	return sorted(set(permitted_users(channel)) | set(system_managers))


def recipients(conversation: str, reason: str) -> list:
	"""Who gets a Notification Log for ``reason``.

	``reason``: ``inbound`` (any inbound message), ``first_inbound`` (first inbound of a
	new or re-opened conversation), ``handoff`` or ``draft_order`` (the AI agent created a
	Draft Sales Order from the chat).

	* assigned conversation -> the assignee, for every reason;
	* unassigned -> the channel's permitted users, but only for ``first_inbound``,
	  ``handoff`` and ``draft_order``.
	"""
	conv = frappe.db.get_value("DB Conversation", conversation, ["assigned_to", "channel"], as_dict=True)
	if not conv:
		return []
	if conv.assigned_to:
		return [conv.assigned_to]
	if reason in BROADCAST_REASONS and conv.channel:
		return permitted_users(conv.channel)
	return []


def notify(conversation: str, reason: str) -> None:
	users = recipients(conversation, reason)
	if not users:
		return
	conv = frappe.db.get_value(
		"DB Conversation",
		conversation,
		["title", "contact_name", "thread_ref", "thread_id", "last_message_preview"],
		as_dict=True,
	)
	who = conv.title or conv.contact_name or conv.thread_ref or conv.thread_id
	if reason == "handoff":
		subject = _("Human needed in conversation with {0}").format(who)
	else:
		subject = _("New message from {0}").format(who)
	for user in users:
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user,
				"type": "Alert",
				"subject": subject,
				"email_content": conv.last_message_preview,
				"document_type": "DB Conversation",
				"document_name": conversation,
			}
		).insert(ignore_permissions=True)


def notify_draft_order(conversation: str, sales_order: str) -> None:
	"""Tell the assignee (else the channel's permitted users) that the AI agent created a
	Draft Sales Order from this chat."""
	users = recipients(conversation, "draft_order")
	if not users:
		return
	conv = frappe.db.get_value(
		"DB Conversation", conversation, ["title", "contact_name", "thread_ref", "thread_id"], as_dict=True
	)
	who = conv.title or conv.contact_name or conv.thread_ref or conv.thread_id
	subject = _("AI agent created draft order {0} in conversation with {1}").format(
		sales_order, escape_html(str(who or ""))
	)
	for user in users:
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user,
				"type": "Alert",
				"subject": subject,
				"document_type": "Sales Order",
				"document_name": sales_order,
			}
		).insert(ignore_permissions=True)
