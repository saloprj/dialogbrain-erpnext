"""Frappe v15 / v16 compatibility helpers.

Aggregates in ``frappe.get_list(fields=...)``:
- v16 accepts only the dict form (``{"COUNT": "*", "as": "cnt"}``) and rejects SQL
  functions written as strings ("SQL functions are not allowed as strings");
- v15's ``DatabaseQuery.sanitize_fields`` calls ``field.lower()`` on every field, so the
  dict form raises AttributeError there; it takes ``"count(name) as cnt"``.
"""

import re

import frappe

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def frappe_major() -> int:
	m = re.search(r"\d+", str(getattr(frappe, "__version__", "") or ""))
	return int(m.group()) if m else 0


def _check(*names):
	for n in names:
		if not _IDENT.match(n or ""):
			raise ValueError(f"unsafe field name: {n!r}")


def count_field(alias: str):
	_check(alias)
	if frappe_major() >= 16:
		return {"COUNT": "*", "as": alias}
	return f"count(name) as {alias}"


def sum_field(field: str, alias: str):
	_check(field, alias)
	if frappe_major() >= 16:
		return {"SUM": field, "as": alias}
	return f"sum({field}) as {alias}"
