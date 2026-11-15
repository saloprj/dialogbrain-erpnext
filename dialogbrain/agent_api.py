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

import math

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, getdate, today

from dialogbrain.compat import sum_field
from dialogbrain.install import AGENT_ROLE, create_roles
from dialogbrain.thread_context import Context, query_string, resolve

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


def _context() -> tuple[Context | None, str | None]:
	"""``(context, reason)`` of this call: the verified thread context (or None) and, when
	the call is not identified, a reason code for the call log (Ruling R8)."""
	return resolve(getattr(frappe.local, "request", None))


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
	detail = getattr(exc, "log_detail", None)
	if detail:  # the caller saw only the generic message; the log keeps the cause
		message = f"{message} ({detail})"
	return f"{type(exc).__name__}: {message}"[:ERROR_LENGTH]


def _log_failure(tool, ctx, exc) -> None:
	"""Log a refused/failed call. Frappe rolls the request back after an exception,
	which would drop the row; re-write it after that rollback and commit it alone."""
	error = _error_text(exc)
	try:
		_log(tool, ctx, 0, error)
	except Exception:
		pass  # a broken transaction must not mask the original exception

	def _after_rollback():
		_log(tool, ctx, 0, error)
		# NB: commit() resets after_rollback, so callbacks queued after this one never run.
		frappe.db.commit()  # nosemgrep: the request already failed; only the log row is written

	frappe.db.after_rollback.add(_after_rollback)


def _run(tool, impl, extra=None, **kwargs):
	"""Guard, refuse a query string, resolve the context, refuse ``extra`` arguments, run
	``impl(ctx, **kwargs) -> (result, count)``, log (with the reason code when the call
	is not identified, Ruling R8)."""
	ctx = None
	try:
		agent_guard()
	except frappe.PermissionError as exc:
		# Logged only for the agent user or an API-key call: any logged-in desk/portal user
		# (or Guest) opening the URL must not fill the log (final wave F6).
		if _refusal_worth_logging():
			_log_failure(tool, None, exc)
		raise
	try:
		_refuse_query_string()
		ctx, reason = _context()
		if extra is not None:
			_refuse_extra(extra)
		result, count = impl(ctx, **kwargs)
	except Exception as exc:
		_log_failure(tool, ctx, exc)
		raise
	_log(tool, ctx, count, reason)
	return result


def _refusal_worth_logging() -> bool:
	"""A guard refusal of the configured agent user (e.g. its role was removed) or of a call
	authenticated with an API key (``Authorization: token …`` / ``Basic …``): someone is
	using a key against the agent API."""
	user = frappe.session.user
	if user and user == frappe.db.get_single_value(SETTINGS, "erpnext_agent_user"):
		return True
	request = getattr(frappe.local, "request", None)
	headers = getattr(request, "headers", None)
	auth = ((headers.get("Authorization") if headers is not None else None) or "").strip().lower()
	return user not in (None, "", "Guest") and auth.startswith(("token ", "basic "))


def _refuse_query_string() -> None:
	"""Ruling R9: Frappe merges ``request.args`` into the tool arguments, and the
	signature does not cover the query string. Arguments come in the JSON body only."""
	request = getattr(frappe.local, "request", None)
	if request is not None and query_string(request):
		raise frappe.ValidationError(_("Query parameters are not accepted; send arguments in the JSON body"))


# Keys Frappe itself puts into ``form_dict`` (``cmd``) or that its JS client adds (``_``
# cache buster, ``csrf_token``). ``ignore_permissions``/``flags`` are dropped by
# ``frappe.get_newargs`` before the call.
FRAPPE_KEYS = frozenset({"cmd", "_", "csrf_token"})


def _refuse_extra(extra: dict) -> None:
	"""Customer tools identify the customer from the signed chat context only. Frappe's
	``get_newargs`` silently drops undeclared kwargs, so each customer tool takes
	``**kwargs`` and refuses anything beyond Frappe's own keys (e.g. ``phone``,
	``email``, ``customer``) instead of ignoring it."""
	unexpected = sorted(k for k in (extra or {}) if k not in FRAPPE_KEYS)
	if unexpected:
		raise frappe.ValidationError(
			_("Unexpected argument(s): {0}; the customer is identified from the chat").format(
				", ".join(unexpected)
			)
		)


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
	# the text is searched literally: "%" or "_" from a chat must not list the catalogue
	like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
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


LEAD_EXISTS = "A lead with these details already exists"


def _create_lead(ctx, name, phone, email, note):
	_require_writes()
	name = (name or "").strip()
	if not name:
		frappe.throw(_("A name is required to create a lead"))
	if ctx and ctx.conversation:
		existing = _chat_lead(ctx.conversation)
		if existing:  # Ruling R7: one Lead per chat
			return {"lead": existing, "existing": True}, 1
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
	messages = len(frappe.local.message_log or [])
	frappe.db.savepoint("dialogbrain_agent_lead")
	try:
		lead.insert(ignore_permissions=True)  # after the guard and the writes switch
	except frappe.DuplicateEntryError:
		# Ruling R7: never reveal the other Lead (ERPNext's message links to it). Undo
		# anything before_insert wrote (e.g. an auto-created Contact) and drop the message.
		frappe.db.rollback(save_point="dialogbrain_agent_lead")
		del frappe.local.message_log[messages:]
		return {"lead": None, "existing": True, "message": _(LEAD_EXISTS)}, 0
	if (note or "").strip():
		lead.add_comment("Comment", text=escape_html(note.strip()))
	if ctx and ctx.conversation:
		_link_unlinked(ctx.conversation, lead.name)
	return {"lead": lead.name}, 1


def _chat_lead(conversation) -> str | None:
	"""The Lead this chat already has: its linked party, else a Lead created from it."""
	conv = frappe.db.get_value("DB Conversation", conversation, ["party_type", "party"], as_dict=True)
	if conv and conv.party_type == "Lead" and conv.party and frappe.db.exists("Lead", conv.party):
		return conv.party
	return frappe.db.get_value("Lead", {"db_conversation": conversation}, "name", order_by="creation asc")


def _link_unlinked(conversation, lead):
	"""Link the chat to the new Lead unless it already has a party (never relink).

	Ruling R6: ``auto_unverified``. The agent acted on what the person typed in the chat,
	so the link neither identifies the customer nor blocks a later re-link."""
	if frappe.db.get_value("DB Conversation", conversation, "party"):
		return
	from dialogbrain.party import _primary_contact
	from dialogbrain.sync.notify import publish

	frappe.db.set_value(
		"DB Conversation",
		conversation,
		{
			"party_type": "Lead",
			"party": lead,
			"contact": _primary_contact("Lead", lead),
			"link_source": "auto_unverified",
		},
	)
	publish(conversation, "conversation", {})


# --- customer-scoped tools (spec §7.3) -----------------------------------------------
#
# The customer is the party of the chat named by the signed thread context, and only
# when that link is ``manual`` or ``auto_verified`` (Ruling R6). No tool takes a phone,
# email or customer argument (``_refuse_extra``). Reads use ``frappe.get_all`` with
# explicit fields, after the guard and the context check (Ruling R5).

NOT_IDENTIFIED = "customer not identified"
PARTY_TYPES = ("Customer", "Lead")
MAX_ORDERS = 5
MAX_INVOICES = 10
MAX_ORDER_LINES = 20
MAX_QTY = 100000
DRAFT_FAILED = "Could not create the draft order"


class DraftOrderFailed(frappe.ValidationError):
	"""The generic error the agent sees when ERPNext refuses the draft; ``log_detail``
	(the real cause) goes to the call log only."""

	log_detail = None


def _not_identified():
	return {"identified": False, "message": _(NOT_IDENTIFIED)}


def _party(ctx):
	"""``(party_type, party)`` of the verified customer of the chat, or None."""
	if not (ctx and ctx.identified and ctx.party and ctx.party_type in PARTY_TYPES):
		return None
	if not frappe.db.exists(ctx.party_type, ctx.party):
		return None
	return ctx.party_type, ctx.party


def _date(value):
	return str(value) if value else None


def _outstanding_totals(customer) -> list:
	rows = frappe.get_all(
		"Sales Invoice",
		filters={"customer": customer, "docstatus": 1, "outstanding_amount": [">", 0]},
		fields=["currency", sum_field("outstanding_amount", "amount")],
		group_by="currency",
		order_by="currency asc",
	)
	return [{"currency": r.currency, "amount": _money(r.amount, r.currency)} for r in rows]


@frappe.whitelist(methods=["POST"])
def find_customer(**kwargs):
	"""Who the verified customer of this chat is, with outstanding totals per currency."""
	return _run("find_customer", _find_customer, extra=kwargs)


def _find_customer(ctx):
	party = _party(ctx)
	if not party:
		return _not_identified(), 0
	party_type, name = party
	if party_type == "Lead":
		lead = frappe.db.get_value("Lead", name, ["lead_name", "company_name"], as_dict=True)
		return {
			"identified": True,
			"customer_name": lead.lead_name or lead.company_name or name,
			"party_type": "Lead",
			"outstanding_total": [],
		}, 1
	return {
		"identified": True,
		"customer_name": frappe.db.get_value("Customer", name, "customer_name") or name,
		"party_type": "Customer",
		"outstanding_total": _outstanding_totals(name),
	}, 1


@frappe.whitelist(methods=["POST"])
def order_status(order_no=None, **kwargs):
	"""The verified customer's last submitted Sales Orders, or the one named ``order_no``
	if it is theirs. Another customer's order number returns nothing."""
	return _run("order_status", _order_status, extra=kwargs, order_no=order_no)


def _order_status(ctx, order_no):
	party = _party(ctx)
	if not party:
		return _not_identified(), 0
	party_type, name = party
	if party_type != "Customer":
		return {"identified": True, "orders": []}, 0
	filters = {"customer": name, "docstatus": 1}
	order_no = (order_no or "").strip() if isinstance(order_no, str) else order_no
	if order_no:
		if not isinstance(order_no, str):
			return {"identified": True, "orders": []}, 0
		filters["name"] = order_no
	rows = frappe.get_all(
		"Sales Order",
		filters=filters,
		fields=["name", "transaction_date", "status", "delivery_date", "per_delivered", "per_billed",
				"grand_total", "currency"],
		order_by="transaction_date desc, creation desc",
		limit=MAX_ORDERS,
	)
	orders = [
		{
			"name": r.name,
			"date": _date(r.transaction_date),
			"status": r.status,
			"delivery_date": _date(r.delivery_date),
			"per_delivered": flt(r.per_delivered, 2),
			"per_billed": flt(r.per_billed, 2),
			"grand_total": _money(r.grand_total, r.currency),
			"currency": r.currency,
		}
		for r in rows
	]
	return {"identified": True, "orders": orders}, len(orders)


@frappe.whitelist(methods=["POST"])
def unpaid_invoices(**kwargs):
	"""The verified customer's submitted Sales Invoices with an outstanding amount."""
	return _run("unpaid_invoices", _unpaid_invoices, extra=kwargs)


def _unpaid_invoices(ctx):
	party = _party(ctx)
	if not party:
		return _not_identified(), 0
	party_type, name = party
	if party_type != "Customer":
		return {"identified": True, "invoices": [], "outstanding_total": []}, 0
	rows = frappe.get_all(
		"Sales Invoice",
		filters={"customer": name, "docstatus": 1, "outstanding_amount": [">", 0]},
		fields=["name", "posting_date", "due_date", "grand_total", "outstanding_amount", "currency"],
		order_by="due_date asc, posting_date asc, name asc",
		limit=MAX_INVOICES,
	)
	invoices = [
		{
			"name": r.name,
			"date": _date(r.posting_date),
			"due_date": _date(r.due_date),
			"grand_total": _money(r.grand_total, r.currency),
			"outstanding_amount": _money(r.outstanding_amount, r.currency),
			"currency": r.currency,
		}
		for r in rows
	]
	return {
		"identified": True,
		"invoices": invoices,
		"outstanding_total": _outstanding_totals(name),
	}, len(invoices)


@frappe.whitelist(methods=["POST"])
def create_draft_order(items=None, **kwargs):
	"""A Draft Sales Order for the verified customer (needs Allow Agent Writes). Never
	submitted; the assigned manager (else the channel's users) gets a notification."""
	return _run("create_draft_order", _create_draft_order, extra=kwargs, items=items)


def _create_draft_order(ctx, items):
	_require_writes()  # Ruling R4
	party = _party(ctx)
	if not party:
		return _not_identified(), 0
	party_type, customer = party
	if party_type != "Customer":
		return {
			"identified": True,
			"order": None,
			"message": _("A draft order needs a customer; this chat is linked to a lead"),
		}, 0
	lines = _order_lines(items)
	company = _company(ctx)
	if not company:
		frappe.throw(_("No company is set for this channel or in DialogBrain Settings"))
	day = today()
	# Frappe would default a new document's currency to the site-wide default (the desk
	# form sets it from the party in JS); use ERPNext's party rule instead.
	currency = frappe.db.get_value("Customer", customer, "default_currency") or frappe.db.get_value(
		"Company", company, "default_currency"
	)
	so = frappe.get_doc(
		{
			"doctype": "Sales Order",
			"customer": customer,
			"company": company,
			"currency": currency,
			"order_type": "Sales",
			"transaction_date": day,
			"delivery_date": day,
			"selling_price_list": frappe.db.get_single_value("Selling Settings", "selling_price_list") or None,
			"db_conversation": ctx.conversation,
			"items": [{"item_code": code, "qty": qty, "delivery_date": day} for code, qty in lines],
		}
	)
	_insert_elevated(so)
	from dialogbrain.sync.notify import notify_draft_order

	notify_draft_order(ctx.conversation, so.name)
	return {
		"identified": True,
		"order": so.name,
		"status": "Draft",
		"grand_total": _money(so.grand_total, so.currency),
		"currency": so.currency,
		"items": [
			{"item_code": i.item_code, "item_name": i.item_name, "qty": flt(i.qty),
			 "rate": _money(i.rate, so.currency)}
			for i in so.items
		],
	}, 1


def _order_lines(items) -> list:
	"""``[(item_code, qty)]`` from ``[{item_code, qty}]``. Only enabled, sales, non-template
	Items; no other keys (the agent never sets a rate, warehouse or discount)."""
	if isinstance(items, str):
		try:
			items = frappe.parse_json(items)
		except ValueError:
			items = None
	if not isinstance(items, list) or not items:
		frappe.throw(_("items must be a non-empty list of {item_code, qty}"), frappe.ValidationError)
	if len(items) > MAX_ORDER_LINES:
		frappe.throw(_("At most {0} items per draft order").format(MAX_ORDER_LINES), frappe.ValidationError)
	lines = []
	for row in items:
		if not isinstance(row, dict):
			frappe.throw(_("Each item must be {item_code, qty}"), frappe.ValidationError)
		unexpected = sorted(set(row) - {"item_code", "qty"})
		if unexpected:
			frappe.throw(_("Unexpected item field(s): {0}; only item_code and qty are accepted").format(
				", ".join(map(str, unexpected))), frappe.ValidationError)
		code = row.get("item_code")
		if not isinstance(code, str) or not code.strip():
			frappe.throw(_("Each item needs an item_code"), frappe.ValidationError)
		lines.append((code.strip(), _qty(row.get("qty"), code)))
	codes = {code for code, _qty in lines}
	sellable = set(
		frappe.get_all(
			"Item",
			filters={"name": ["in", list(codes)], "disabled": 0, "is_sales_item": 1, "has_variants": 0},
			pluck="name",
		)
	)
	missing = sorted(codes - sellable)
	if missing:
		frappe.throw(_("Unknown or unavailable item(s): {0}").format(", ".join(missing)), frappe.ValidationError)
	return lines


def _qty(value, code) -> float:
	"""A finite real quantity, 0 < qty <= MAX_QTY: an int, a float or a numeric string;
	never a bool, NaN or infinity."""
	qty = None
	if isinstance(value, bool):
		qty = None
	elif isinstance(value, (int, float)):
		try:
			qty = float(value)
		except OverflowError:
			qty = None
	elif isinstance(value, str):
		try:
			qty = float(value.strip())
		except ValueError:
			qty = None
	if qty is None or not math.isfinite(qty) or not 0 < qty <= MAX_QTY:
		frappe.throw(
			_("Quantity for {0} must be a number greater than 0 and at most {1}").format(code, MAX_QTY),
			frappe.ValidationError,
		)
	return qty


def _insert_elevated(doc) -> None:
	"""Insert ``doc`` (ignore_permissions) as Administrator, then record the agent user as
	its owner.

	ERPNext's own validation reads Item / Account / Warehouse with the session user's
	permissions (``item.check_permission()``, ``check_account_permission``), and the agent
	role has none by design (Ruling R5). This runs only after the guard, the
	``allow_agent_writes`` switch, the context check and ``_order_lines``.

	Ruling R9: nothing ERPNext says inside the elevated window reaches the caller. Messages
	queued during the insert are dropped, and a failure becomes :class:`DraftOrderFailed`
	with a generic message; the cause goes to the call log only.
	"""
	agent = frappe.session.user
	messages = len(frappe.local.message_log or [])
	frappe.set_user("Administrator")
	try:
		doc.insert(ignore_permissions=True)
	except Exception as exc:  # noqa: BLE001 - every cause becomes the generic error (R9)
		failed = DraftOrderFailed(_(DRAFT_FAILED))
		failed.log_detail = _error_text(exc)
		raise failed from None
	finally:
		frappe.set_user(agent)
		if frappe.local.message_log:
			del frappe.local.message_log[messages:]
	frappe.db.set_value(doc.doctype, doc.name, {"owner": agent, "modified_by": agent}, update_modified=False)
	doc.owner = doc.modified_by = agent
