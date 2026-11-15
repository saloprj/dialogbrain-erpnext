"""Realtime events and Notification Log alerts for DialogBrain conversations."""

import frappe
from frappe import _

from dialogbrain.permissions import permitted_users

REALTIME_PREFIX = "dialogbrain:"


def publish(conversation: str, kind: str, payload: dict) -> None:
	"""Send ``dialogbrain:<kind>`` to every user permitted on the conversation's channel."""
	channel = frappe.db.get_value("DB Conversation", conversation, "channel")
	if not channel:
		return
	payload = {"conversation": conversation, **(payload or {})}
	for user in permitted_users(channel):
		frappe.publish_realtime(REALTIME_PREFIX + kind, payload, user=user, after_commit=True)


def recipients(conversation: str, reason: str) -> list:
	"""Who gets a Notification Log for ``reason``.

	``reason``: ``inbound`` (any inbound message), ``first_inbound`` (first inbound of a
	new or re-opened conversation) or ``handoff``.

	* assigned conversation -> the assignee, for every reason;
	* unassigned -> the channel's permitted users, but only for ``first_inbound`` and
	  ``handoff``.
	"""
	conv = frappe.db.get_value("DB Conversation", conversation, ["assigned_to", "channel"], as_dict=True)
	if not conv:
		return []
	if conv.assigned_to:
		return [conv.assigned_to]
	if reason in ("first_inbound", "handoff") and conv.channel:
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
