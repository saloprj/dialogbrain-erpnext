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


def only_for(roles) -> None:
	"""``frappe.only_for(roles)``: raise ``frappe.PermissionError`` unless the user has one of
	``roles`` (Administrator always passes).

	Frappe v15's ``only_for`` returns early whenever ``frappe.flags.in_test`` is set, so on v15
	the role check would not run under tests; this applies it there too (v16 has no bypass)."""
	if frappe_major() >= 16:
		frappe.only_for(roles)
		return
	if frappe.session.user == "Administrator":
		return
	if isinstance(roles, str):
		roles = (roles,)
	if set(roles).isdisjoint(frappe.get_roles()):
		frappe.throw(frappe._("Not permitted"), frappe.PermissionError)


def single_values(doctype: str, fields) -> "frappe._dict":
	"""``frappe.db.get_value(doctype, doctype, fields, as_dict=True)`` for a Single, typed.

	v16 casts Singles values by field type; v15 returns them as the raw strings stored in
	``tabSingles`` (a Check of ``"0"`` is truthy there). Empty values stay ``None``."""
	values = frappe.db.get_value(doctype, doctype, list(fields), as_dict=True) or frappe._dict()
	if frappe_major() >= 16:
		return values
	from frappe.utils.data import cast

	meta = frappe.get_meta(doctype)
	for field, value in values.items():
		df = meta.get_field(field)
		if df and value is not None:
			values[field] = cast(df.fieldtype, value)
	return values


def other_apps_translations(lang: str) -> dict:
	"""Translations the installed apps other than dialogbrain provide for ``lang``.

	Keys are the source text, or ``source:context`` for contextual rows. v15 reads CSV files
	only, v16 also gettext MO files; ``get_translations_from_apps`` covers both when it is
	given the app list, and the CSV reader is the fallback for a Frappe without that argument."""
	import frappe.translate as translate

	apps = [app for app in frappe.get_installed_apps() if app != "dialogbrain"]
	try:
		return dict(translate.get_translations_from_apps(lang, apps))
	except TypeError:
		merged = {}
		for app in apps:
			merged.update(translate.get_translations_from_csv(lang, app) or {})
		return merged
