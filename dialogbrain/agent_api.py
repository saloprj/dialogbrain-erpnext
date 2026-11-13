"""Tools the DialogBrain AI agent calls over HTTP (Plan 3, spec §7.3).

DialogBrain registers each method as an HTTP integration endpoint
(``POST /api/method/dialogbrain.agent_api.<method>``) and authenticates as the agent user
(``Settings.erpnext_agent_user``) with that user's API key.

Access model:

* :func:`agent_guard` lets only the agent user, holding the ``DialogBrain Agent API``
  role, through. Any other session gets PermissionError, including Administrator, desk
  users, Guest, and an agent user of another site.
* The role grants **no document permissions**. Every read and write is app code, run
  after the guard and the tool's own scope checks, with explicit field lists. So the
  agent key cannot reach ``/api/resource`` for anything, such as buying prices, stock
  valuation or other customers' orders. It also cannot insert Leads / Sales Orders that
  bypass ``allow_agent_writes``.
* Write tools refuse unless ``Settings.allow_agent_writes`` is on (Ruling R4).
  DialogBrain's ``denied_tools`` is a convenience, not the boundary.
* Every call writes a ``DB Agent Call Log`` row, including refused and failed calls
  (those rows survive the request's rollback).

Output is compact JSON for an LLM: document names only, and amounts rounded to the
currency precision.
"""

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, getdate, today

from dialogbrain.compat import sum_field
from dialogbrain.install import AGENT_ROLE, create_roles
from dialogbrain.thread_context import Context, verify

SETTINGS = "DialogBrain Settings"
LOG = "DB Agent Call Log"
AGENT_USER_LOCAL_PART = "dialogbrain-agent"
DEFAULT_LIMIT = 5
MAX_LIMIT = 20
ERROR_LENGTH = 500


# --- agent user ----------------------------------------------------------------------


def agent_user_name() -> str:
	"""``dialogbrain-agent@<site>``. A site name without a dot (e.g. ``frontend``) gets
	``.local``, because Frappe rejects an email address whose domain has no dot."""
	host = (frappe.local.site or "localhost").strip().lower()
	if "." not in host:
		host += ".local"
	return f"{AGENT_USER_LOCAL_PART}@{host}"


def ensure_agent_user() -> str:
	"""Create (or repair) the agent user and record it in Settings. Idempotent.

	The user is an enabled System User whose only role is ``DialogBrain Agent API``.
	Any other role found on it is removed, so it never gains desk access to data.
	"""
	create_roles()
	name = agent_user_name()
	if frappe.db.exists("User", name):
		user = frappe.get_doc("User", name)
		changed = False
		if {r.role for r in user.roles} != {AGENT_ROLE}:
			user.set("roles", [{"role": AGENT_ROLE}])
			changed = True
		if not user.enabled:
			user.enabled = 1
			changed = True
		if changed:
			user.save(ignore_permissions=True)
	else:
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": name,
				"first_name": "DialogBrain Agent",
				"enabled": 1,
				"user_type": "System User",
				"send_welcome_email": 0,
				"roles": [{"role": AGENT_ROLE}],
			}
		)
		user.flags.no_welcome_mail = True
		user.insert(ignore_permissions=True)
	frappe.db.set_single_value(SETTINGS, "erpnext_agent_user", name)
	return name


def agent_guard() -> None:
	"""Raise PermissionError unless the session is the configured agent user with the role."""
	user = frappe.session.user
	expected = frappe.db.get_single_value(SETTINGS, "erpnext_agent_user")
	if not expected or user != expected or AGENT_ROLE not in frappe.get_roles(user):
		raise frappe.PermissionError(_("Only the DialogBrain agent user may call the agent API"))


# --- plumbing ------------------------------------------------------------------------


def _context() -> Context | None:
	"""The verified thread context of this call, or None (B2: signed headers)."""
	return verify(getattr(frappe.local, "request", None))


def _log(tool, ctx, result_count, error=None) -> str:
	doc = frappe.get_doc(
		{
			"doctype": LOG,
			"tool": tool,
			"thread_id": ctx.thread_id if ctx else None,
			"conversation": ctx.conversation if ctx else None,
			"party_type": ctx.party_type if ctx else None,
			"party": ctx.party if ctx else None,
			"identified": 1 if ctx and ctx.identified else 0,
			"result_count": cint(result_count),
			"error": error,
		}
	)
	doc.flags.ignore_links = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _error_text(exc) -> str:
	message = str(exc).strip() or type(exc).__name__
	return f"{type(exc).__name__}: {message}"[:ERROR_LENGTH]


def _log_failure(tool, ctx, exc) -> None:
	"""Log a refused/failed call. Frappe rolls the request back after an exception,
	which would drop the row; re-write it after that rollback and commit it alone."""
	error = _error_text(exc)
	_log(tool, ctx, 0, error)

	def _after_rollback():
		_log(tool, ctx, 0, error)
		frappe.db.commit()  # nosemgrep: the request already failed; only the log row is written

	frappe.db.after_rollback.add(_after_rollback)


def _run(tool, impl, **kwargs):
	"""Guard, resolve the context, run ``impl(ctx, **kwargs) -> (result, count)``, log."""
	ctx = None
	try:
		agent_guard()
		ctx = _context()
		result, count = impl(ctx, **kwargs)
	except Exception as exc:
		_log_failure(tool, ctx, exc)
		raise
	_log(tool, ctx, count)
	return result


def _require_writes() -> None:
	if not cint(frappe.db.get_single_value(SETTINGS, "allow_agent_writes")):
		raise frappe.PermissionError(
			_("Agent writes are disabled in DialogBrain Settings (Allow Agent Writes is off)")
		)


def _money(amount, currency):
	from frappe.model.meta import get_field_precision

	if amount is None:
		return None
	df = frappe._dict(fieldtype="Currency", fieldname="amount")
	return flt(amount, get_field_precision(df, currency=currency))


def _company(ctx) -> str | None:
	"""The company of the chat's channel, else ``Settings.default_company``."""
	if ctx and ctx.conversation:
		channel = frappe.db.get_value("DB Conversation", ctx.conversation, "channel")
		company = channel and frappe.db.get_value("DB Channel", channel, "company")
		if company:
			return company
	return frappe.db.get_single_value(SETTINGS, "default_company") or None


# --- generic tools -------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def get_item(query=None, limit=DEFAULT_LIMIT):
	"""Find enabled sales items by code, name or description, with price and stock."""
	return _run("get_item", _get_item, query=query, limit=limit)


def _get_item(ctx, query, limit):
	query = (query or "").strip()
	limit = min(max(cint(limit) or DEFAULT_LIMIT, 1), MAX_LIMIT)
	if not query:
		return {"items": []}, 0
	like = f"%{query}%"
	rows = frappe.get_all(
		"Item",
		filters={"disabled": 0, "is_sales_item": 1, "has_variants": 0},
		or_filters=[["item_code", "like", like], ["item_name", "like", like], ["description", "like", like]],
		fields=["item_code", "item_name", "is_stock_item"],
		order_by="item_name asc, item_code asc",
		limit=limit,
	)
	codes = [r.item_code for r in rows]
	prices = _prices(codes)
	stock = _stock([r.item_code for r in rows if r.is_stock_item], _company(ctx))
	items = []
	for r in rows:
		price = prices.get(r.item_code)
		if r.is_stock_item:
			qty = flt(stock.get(r.item_code))
			in_stock = qty > 0
		else:
			qty, in_stock = None, True
		items.append(
			{
				"item_code": r.item_code,
				"item_name": r.item_name,
				"price": _money(price.price_list_rate, price.currency) if price else None,
				"currency": price.currency if price else None,
				"stock_qty": qty,
				"in_stock": in_stock,
			}
		)
	return {"items": items}, len(items)


def _prices(codes) -> dict:
	"""item_code -> the current general (no customer) Item Price on the default selling list."""
	price_list = frappe.db.get_single_value("Selling Settings", "selling_price_list")
	if not codes or not price_list:
		return {}
	now = getdate(today())
	out = {}
	for row in frappe.get_all(
		"Item Price",
		filters={"item_code": ["in", codes], "price_list": price_list},
		fields=["item_code", "price_list_rate", "currency", "customer", "valid_from", "valid_upto"],
		order_by="valid_from desc, modified desc",
	):
		if row.item_code in out or row.customer:
			continue
		if row.valid_from and getdate(row.valid_from) > now:
			continue
		if row.valid_upto and getdate(row.valid_upto) < now:
			continue
		out[row.item_code] = row
	return out


def _stock(codes, company) -> dict:
	"""item_code -> sum of Bin.actual_qty over the company's warehouses (all warehouses
	when no company is known)."""
	if not codes:
		return {}
	filters = {"item_code": ["in", codes]}
	if company:
		warehouses = frappe.get_all("Warehouse", filters={"company": company}, pluck="name")
		if not warehouses:
			return {}
		filters["warehouse"] = ["in", warehouses]
	rows = frappe.get_all(
		"Bin", filters=filters, fields=["item_code", sum_field("actual_qty", "qty")], group_by="item_code"
	)
	return {r.item_code: r.qty for r in rows}


@frappe.whitelist(methods=["POST"])
def create_lead(name=None, phone=None, email=None, note=None):
	"""Create a Lead for the person in the chat (needs Allow Agent Writes)."""
	return _run("create_lead", _create_lead, name=name, phone=phone, email=email, note=note)


def _create_lead(ctx, name, phone, email, note):
	_require_writes()
	name = (name or "").strip()
	if not name:
		frappe.throw(_("A name is required to create a lead"))
	lead = frappe.get_doc(
		{
			"doctype": "Lead",
			"first_name": name[:140],
			"mobile_no": (phone or "").strip() or None,
			"email_id": (email or "").strip() or None,
			"source": "Chat" if frappe.db.exists("Lead Source", "Chat") else None,
			"db_conversation": ctx.conversation if ctx else None,
		}
	)
	lead.insert(ignore_permissions=True)  # after the guard and the writes switch
	if (note or "").strip():
		lead.add_comment("Comment", text=escape_html(note.strip()))
	if ctx and ctx.conversation:
		_link_unlinked(ctx.conversation, lead.name)
	return {"lead": lead.name}, 1


def _link_unlinked(conversation, lead):
	"""Link the chat to the new Lead unless it already has a party (never relink)."""
	if frappe.db.get_value("DB Conversation", conversation, "party"):
		return
	frappe.db.set_value(
		"DB Conversation", conversation, {"party_type": "Lead", "party": lead, "link_source": "manual"}
	)
	from dialogbrain.sync.notify import publish

	publish(conversation, "conversation", {})
