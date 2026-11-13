"""Customer card for the inbox side panel (Plan 2, Task 4).

Order / invoice data is ERPNext data: it is shown only to users who hold read access to
Sales Order / Sales Invoice (``frappe.get_list`` raises PermissionError otherwise, so the
check happens first). ``has_access`` tells the UI what to hide.

``stats`` is ``{orders_count, totals: [{currency, total, count}, ...]}``: submitted Sales
Orders grouped by currency (a sum across currencies would be meaningless), largest total
first.

Client-side prefill (Task 6): Sales Order / Quotation open with
``frappe.new_doc(doctype, {customer | party_name, ...})``; Lead creation reuses
``dialogbrain.party.create_lead``.
"""

import frappe
from frappe import _

from dialogbrain import compat
from dialogbrain import party as party_mod
from dialogbrain.permissions import assert_can_access

RECENT_LIMIT = 5
SEARCH_LIMIT = 10


def _party_name(party_type, party):
	field = "lead_name" if party_type == "Lead" else "customer_name"
	return frappe.db.get_value(party_type, party, field) or party


def _contact(conv):
	name = conv.contact
	if not name and conv.party_type and conv.party:
		name = party_mod._primary_contact(conv.party_type, conv.party)
	phone, email = conv.phone, conv.email
	if name and frappe.db.exists("Contact", name):
		c = frappe.db.get_value("Contact", name, ["mobile_no", "phone", "email_id"], as_dict=True)
		phone = phone or c.mobile_no or c.phone
		email = email or c.email_id
	return {"name": name, "phone": phone, "email": email}


def _orders(customer):
	rows = frappe.get_list(
		"Sales Order",
		filters={"customer": customer, "docstatus": 1},
		fields=["name", "transaction_date", "grand_total", "status", "currency"],
		order_by="transaction_date desc, creation desc",
		limit=RECENT_LIMIT,
	)
	grouped = frappe.get_list(
		"Sales Order",
		filters={"customer": customer, "docstatus": 1},
		fields=[
			"currency",
			compat.count_field("cnt"),
			compat.sum_field("grand_total", "total"),
		],
		group_by="currency",
	)
	totals = sorted(
		(
			{"currency": g.get("currency"), "total": float(g.get("total") or 0), "count": int(g.get("cnt") or 0)}
			for g in grouped
		),
		key=lambda t: t["total"],
		reverse=True,
	)
	stats = {"orders_count": sum(t["count"] for t in totals), "totals": totals}
	return stats, rows


def _unpaid(customer):
	return frappe.get_list(
		"Sales Invoice",
		filters={"customer": customer, "docstatus": 1, "outstanding_amount": [">", 0]},
		fields=["name", "due_date", "outstanding_amount", "currency"],
		order_by="due_date asc, creation desc",
		limit=RECENT_LIMIT,
	)


@frappe.whitelist()
def get_card(conversation):
	assert_can_access(conversation)
	conv = frappe.db.get_value(
		"DB Conversation",
		conversation,
		["party_type", "party", "contact", "phone", "email", "link_source"],
		as_dict=True,
	)
	has_party = bool(conv.party_type and conv.party and frappe.db.exists(conv.party_type, conv.party))
	card = {
		"party_type": conv.party_type if has_party else None,
		"party": conv.party if has_party else None,
		"party_name": _party_name(conv.party_type, conv.party) if has_party else None,
		"link_source": conv.link_source,
		"contact": _contact(conv),
		"has_access": {
			"orders": bool(frappe.has_permission("Sales Order", "read")),
			"invoices": bool(frappe.has_permission("Sales Invoice", "read")),
		},
		"stats": None,
		"recent_orders": [],
		"unpaid_invoices": [],
		"lead": None,
		"suggestions": [],
	}
	if not has_party:
		card["suggestions"] = [
			{"party_type": t, "party": p, "matched_by": m, "party_name": _party_name(t, p)}
			for t, p, m in party_mod.find_parties(conv.phone, conv.email)
		]
		return card

	if conv.party_type == "Lead":
		lead = frappe.db.get_value("Lead", conv.party, ["status", "company_name"], as_dict=True)
		card["lead"] = {"name": conv.party, "status": lead.status, "company_name": lead.company_name}
		return card

	if card["has_access"]["orders"]:
		card["stats"], card["recent_orders"] = _orders(conv.party)
	if card["has_access"]["invoices"]:
		card["unpaid_invoices"] = _unpaid(conv.party)
	return card


@frappe.whitelist()
def search_parties(conversation, txt):
	assert_can_access(conversation)
	txt = (txt or "").strip()
	if len(txt) < 2:
		return []
	like = f"%{txt}%"
	specs = (
		("Customer", "customer_name", ["customer_name", "mobile_no", "email_id"]),
		("Lead", "lead_name", ["lead_name", "mobile_no", "phone", "email_id", "company_name"]),
	)
	out = []
	for doctype, title_field, fields in specs:
		if len(out) >= SEARCH_LIMIT or not frappe.has_permission(doctype, "read"):
			continue
		rows = frappe.get_list(
			doctype,
			or_filters=[[doctype, f, "like", like] for f in fields],
			fields=["name", f"{title_field} as title", "mobile_no", "email_id"],
			limit=SEARCH_LIMIT - len(out),
			order_by=f"{title_field} asc",
		)
		out += [
			{"party_type": doctype, "party": r.name, "title": r.title, "phone": r.mobile_no, "email": r.email_id}
			for r in rows
		]
	return out
