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
		frappe.throw(frappe._("Not permitted", context="DialogBrain"), frappe.PermissionError)


def single_values(doctype: str, fields) -> "frappe._dict":
	"""The Single's ``fields`` as a ``frappe._dict``, typed; a field never stored is absent.

	Read with ``frappe.db.get_singles_dict`` (the Singles API; ``frappe.db.get_value`` on a
	Single is not type safe on v15), raw, then only the requested fields are cast: on v16
	exactly as its ``get_value`` did; on v15 (raw strings in ``tabSingles``, a Check of
	``"0"`` is truthy there) with ``cast``, and empty values stay ``None``."""
	fields = list(fields)
	stored = frappe.db.get_singles_dict(doctype)  # raw: a bad unrelated value cannot raise
	values = frappe._dict({f: stored[f] for f in fields if f in stored})
	meta = frappe.get_meta(doctype)
	from frappe.utils.data import cast

	if frappe_major() >= 16:
		# as v16's get_value on a Single (its database.py imports frappe.utils.cast as
		# cast_fieldtype): every stored value cast, None included, get_default_df fallback
		from frappe.model.meta import get_default_df

		for field, value in values.items():
			df = meta.get_field(field) or get_default_df(field)
			if df:
				values[field] = cast(df.fieldtype, value)
		return values
	for field, value in values.items():
		df = meta.get_field(field)
		if df and value is not None:
			values[field] = cast(df.fieldtype, value)
	return values


def other_apps_translations(lang: str) -> dict:
	"""Translations the installed apps other than dialogbrain provide for ``lang``.

	Keys are the source text, or ``source:context`` for contextual rows. Old v15 releases read
	CSV files only; recent v15 and v16 also read the compiled gettext MO files (a full
	``bench build`` or ``compile-po-to-mo`` makes them); ``get_translations_from_apps`` covers both when it is
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


def rate_limited(key: str, limit: int, seconds: int) -> bool:
	"""Count one more use of ``key``; True when that is over ``limit`` within ``seconds``.

	A plain redis counter: ``frappe.rate_limiter.rate_limit(user_based=...)`` is not in every
	v15 release, and it limits per endpoint, not per our own key (a claim, a user)."""
	cache = frappe.cache()
	name = cache.make_key(f"dialogbrain_rl:{key}")
	count = cache.incr(name)
	if count == 1 or cache.ttl(name) < 0:  # < 0: a counter left without its expiry
		cache.expire(name, seconds)
	return count > limit
