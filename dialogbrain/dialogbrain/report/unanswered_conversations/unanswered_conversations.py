import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from dialogbrain.dialogbrain.report import assert_manager


def execute(filters=None):
	assert_manager()
	filters = frappe._dict(filters or {})
	return get_columns(), get_data(filters)


def get_columns():
	return [
		{"label": _("Conversation"), "fieldname": "conversation", "fieldtype": "Link",
		 "options": "DB Conversation", "width": 160},
		{"label": _("Title", context="DialogBrain"), "fieldname": "title", "fieldtype": "Data", "width": 180},
		{"label": _("Channel"), "fieldname": "channel", "fieldtype": "Link", "options": "DB Channel", "width": 140},
		{"label": _("Assigned to"), "fieldname": "assigned_to", "fieldtype": "Link", "options": "User",
		 "width": 160},
		{"label": _("Last inbound at"), "fieldname": "last_inbound_at", "fieldtype": "Datetime", "width": 160},
		{"label": _("Waiting (min)"), "fieldname": "waiting_min", "fieldtype": "Int", "width": 110},
		{"label": _("Unread", context="DialogBrain"), "fieldname": "unread_count", "fieldtype": "Int", "width": 80},
		{"label": _("Last message preview"), "fieldname": "last_message_preview", "fieldtype": "Data",
		 "width": 300},
	]


def get_data(filters):
	conditions = ["status = 'Open'", "last_message_direction = 'in'"]
	values = {"now": now_datetime()}
	if filters.channel:
		conditions.append("channel = %(channel)s")
		values["channel"] = filters.channel
	if filters.assigned_to:
		conditions.append("assigned_to = %(assigned_to)s")
		values["assigned_to"] = filters.assigned_to
	min_waiting = cint(filters.min_waiting_minutes)
	if min_waiting > 0:
		conditions.append("TIMESTAMPDIFF(MINUTE, last_inbound_at, %(now)s) >= %(min_waiting)s")
		values["min_waiting"] = min_waiting
	return frappe.db.sql(
		f"""SELECT name AS conversation, title, channel, assigned_to, last_inbound_at,
			COALESCE(TIMESTAMPDIFF(MINUTE, last_inbound_at, %(now)s), 0) AS waiting_min,
			unread_count, last_message_preview
		FROM `tabDB Conversation`
		WHERE {" AND ".join(conditions)}
		ORDER BY last_inbound_at IS NULL, last_inbound_at ASC, name ASC""",  # noqa: S608 - fixed text, values bound
		values,
		as_dict=True,
	)
