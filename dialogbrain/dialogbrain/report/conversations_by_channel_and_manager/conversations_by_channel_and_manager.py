import frappe
from frappe import _

from dialogbrain.dialogbrain.report import assert_manager, group_label, label_unassigned, period

# group_by filter value -> SQL expression over `tabDB Conversation` aliased ``c`` (fixed here)
GROUPS = {
	"Channel": "c.channel",
	"Manager": "COALESCE(NULLIF(c.assigned_to, ''), 'Unassigned')",
}
FIELDS = ("opened", "closed", "open_now", "unanswered_now", "inbound", "outbound", "by_ai")


def execute(filters=None):
	assert_manager()
	filters = frappe._dict(filters or {})
	group_by = filters.group_by if filters.group_by in GROUPS else "Channel"
	return get_columns(group_by), label_unassigned(get_data(filters, group_by))


def get_columns(group_by):
	def col(label, fieldname):
		return {"label": label, "fieldname": fieldname, "fieldtype": "Int", "width": 120}

	return [
		{"label": group_label(group_by), "fieldname": "group", "fieldtype": "Data", "width": 200},
		col(_("Opened"), "opened"),
		col(_("Closed"), "closed"),
		col(_("Open now"), "open_now"),
		col(_("Unanswered now"), "unanswered_now"),
		col(_("Inbound messages"), "inbound"),
		col(_("Outbound messages"), "outbound"),
		col(_("By AI"), "by_ai"),
	]


def get_data(filters, group_by):
	grp = GROUPS[group_by]
	start, end = period(filters)
	values = {"start": start, "end": end}
	groups = {}

	def merge(rows):
		for row in rows:
			item = groups.setdefault(str(row.grp), {"group": str(row.grp), **dict.fromkeys(FIELDS, 0)})
			for key in FIELDS:
				item[key] += int(row.get(key) or 0)

	# opened / closed in the period, and the current state (not period-bound)
	merge(frappe.db.sql(
		f"""SELECT {grp} AS grp,
			SUM(c.opened_at >= %(start)s AND c.opened_at < %(end)s) AS opened,
			SUM(c.closed_at >= %(start)s AND c.closed_at < %(end)s) AS closed,
			SUM(c.status = 'Open') AS open_now,
			SUM(c.status = 'Open' AND c.last_message_direction = 'in') AS unanswered_now
		FROM `tabDB Conversation` c
		GROUP BY grp""",  # noqa: S608 - identifiers come from GROUPS, values are parameters
		values,
		as_dict=True,
	))
	# messages sent in the period, joined to their conversation
	merge(frappe.db.sql(
		f"""SELECT {grp} AS grp,
			SUM(m.direction = 'in') AS inbound,
			SUM(m.direction = 'out') AS outbound,
			SUM(m.direction = 'out' AND m.author_type = 'agent') AS by_ai
		FROM `tabDB Message` m
		INNER JOIN `tabDB Conversation` c ON c.name = m.conversation
		WHERE m.sent_at >= %(start)s AND m.sent_at < %(end)s
		GROUP BY grp""",  # noqa: S608 - identifiers come from GROUPS, values are parameters
		values,
		as_dict=True,
	))
	# only groups with something to show (an empty period yields no rows)
	return sorted(
		(item for item in groups.values() if item["opened"] or item["closed"] or item["inbound"]
		 or item["outbound"]),
		key=lambda item: item["group"],
	)
