import statistics

import frappe
from frappe import _

from dialogbrain.dialogbrain.report import assert_manager, minutes, period

# group_by filter value -> SQL expression (fixed here, never taken from the request)
GROUPS = {
	"Manager": "COALESCE(NULLIF(assigned_to, ''), 'Unassigned')",
	"Channel": "channel",
	"Day": "DATE(opened_at)",
}
GROUP_LABELS = {"Manager": _("Manager"), "Channel": _("Channel"), "Day": _("Day")}


def execute(filters=None):
	assert_manager()
	filters = frappe._dict(filters or {})
	group_by = filters.group_by if filters.group_by in GROUPS else "Manager"
	return get_columns(group_by), get_data(filters, group_by)


def get_columns(group_by):
	return [
		{"label": _(group_by), "fieldname": "group", "fieldtype": "Data", "width": 200},
		{"label": _("Conversations"), "fieldname": "conversations", "fieldtype": "Int", "width": 120},
		{"label": _("Answered"), "fieldname": "answered", "fieldtype": "Int", "width": 100},
		{"label": _("Not answered yet"), "fieldname": "not_answered", "fieldtype": "Int", "width": 130},
		{"label": _("Average (min)"), "fieldname": "avg_min", "fieldtype": "Float", "precision": 1, "width": 120},
		{"label": _("Median (min)"), "fieldname": "median_min", "fieldtype": "Float", "precision": 1, "width": 120},
		{"label": _("Longest (min)"), "fieldname": "longest_min", "fieldtype": "Float", "precision": 1, "width": 120},
		{"label": _("Within 5 min (%)"), "fieldname": "within_5_pct", "fieldtype": "Float", "precision": 1, "width": 130},
	]


def get_data(filters, group_by):
	start, end = period(filters)
	conditions = ["opened_at >= %(start)s", "opened_at < %(end)s"]
	values = {"start": start, "end": end}
	if filters.channel:
		conditions.append("channel = %(channel)s")
		values["channel"] = filters.channel
	if filters.assigned_to:
		conditions.append("assigned_to = %(assigned_to)s")
		values["assigned_to"] = filters.assigned_to
	rows = frappe.db.sql(
		f"""SELECT {GROUPS[group_by]} AS grp, first_response_seconds
		FROM `tabDB Conversation`
		WHERE {" AND ".join(conditions)}
		ORDER BY grp""",  # noqa: S608 - identifiers come from GROUPS, values are parameters
		values,
		as_dict=True,
	)
	groups = {}
	for row in rows:
		groups.setdefault(str(row.grp), []).append(row.first_response_seconds or 0)

	data = []
	for group, seconds in groups.items():
		answered = [s for s in seconds if s > 0]
		item = {
			"group": group,
			"conversations": len(seconds),
			"answered": len(answered),
			"not_answered": len(seconds) - len(answered),
			"avg_min": None,
			"median_min": None,
			"longest_min": None,
			"within_5_pct": None,
		}
		if answered:
			item["avg_min"] = minutes(sum(answered) / len(answered))
			item["median_min"] = minutes(statistics.median(answered))
			item["longest_min"] = minutes(max(answered))
			item["within_5_pct"] = round(100.0 * sum(1 for s in answered if s <= 300) / len(answered), 1)
		data.append(item)
	return data
