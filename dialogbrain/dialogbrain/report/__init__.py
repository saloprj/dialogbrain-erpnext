"""Shared helpers of the DialogBrain script reports."""

import frappe
from frappe import _
from frappe.utils import add_days, getdate, nowdate

from dialogbrain.permissions import is_manager


def assert_manager():
	"""Reports are for Chat Manager / System Manager only, whatever path calls ``execute``."""
	if not is_manager():
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def period(filters):
	"""(start, end_exclusive) datetimes-as-strings of the filter period; default last 30 days."""
	to_date = getdate(filters.get("to_date") or nowdate())
	from_date = getdate(filters.get("from_date") or add_days(to_date, -30))
	return str(from_date), str(add_days(to_date, 1))


def minutes(seconds):
	return round(seconds / 60.0, 1)


def group_label(group_by):
	"""Column title of a group-by value; literal ``_()`` calls run per request, in the user's language."""
	if group_by == "Manager":
		return _("Manager")
	if group_by == "Day":
		return _("Day")
	return _("Channel")


def label_unassigned(rows):
	"""The SQL group value ``Unassigned`` is shown in the user's language."""
	for row in rows:
		if row.get("group") == "Unassigned":
			row["group"] = _("Unassigned")
	return rows
