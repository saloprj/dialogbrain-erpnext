"""Tests for dialogbrain.api.party_card.

Shortcut: Sales Orders / Sales Invoices are inserted with ``db_insert`` (submitted rows,
no Company/Item/Warehouse/GL setup): the test site has no ERPNext setup data and the card
only reads a handful of columns. Customers and Leads are real documents.
"""

import frappe
from frappe.utils import add_days, today

from dialogbrain.api import party_card as api
from dialogbrain.tests.test_inbox_api import _channel, _conv, _user
from dialogbrain.tests.utils import DialogBrainTestCase

CH_A = "card-a"
CH_B = "card-b"


def _customer(name):
	if frappe.db.exists("Customer", name):
		return name
	return frappe.get_doc(
		{"doctype": "Customer", "customer_name": name, "customer_type": "Individual"}
	).insert(ignore_permissions=True).name


def _lead(first, phone=None):
	return frappe.get_doc(
		{"doctype": "Lead", "first_name": first, "mobile_no": phone, "company_name": "Acme"}
	).insert(ignore_permissions=True).name


def _raw(doctype, **fields):
	doc = frappe.get_doc({"doctype": doctype, "name": frappe.generate_hash(length=10), **fields})
	doc.db_insert()
	return doc.name


def _so(customer, total, docstatus=1, date=None):
	return _raw("Sales Order", customer=customer, grand_total=total, docstatus=docstatus,
				currency="USD", status="To Deliver and Bill" if docstatus else "Draft",
				transaction_date=date or today())


def _si(customer, outstanding, docstatus=1, due=None):
	return _raw("Sales Invoice", customer=customer, outstanding_amount=outstanding,
				docstatus=docstatus, currency="USD", due_date=due or today())


class TestPartyCard(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for dt in ("DB Message", "DB Conversation", "DB Channel User", "DB Channel"):
			frappe.db.delete(dt)
		frappe.db.delete("Sales Order", {"customer": ["like", "Card %"]})
		frappe.db.delete("Sales Invoice", {"customer": ["like", "Card %"]})
		self.sales = _user("card-sales@example.com", "Chat Agent", "Sales User", "Accounts User")
		self.plain = _user("card-plain@example.com", "Chat Agent")
		self.other = _user("card-other@example.com", "Chat Agent")
		_channel(CH_A, "telegram", [self.sales, self.plain])
		_channel(CH_B, "whatsapp", [self.other])
		self.cust = _customer("Card Customer")

	def tearDown(self):
		frappe.set_user("Administrator")

	def _linked(self, party_type, party, **fields):
		return _conv(CH_A, party_type=party_type, party=party, link_source="manual", **fields)

	def test_customer_card_stats_and_lists(self):
		_so(self.cust, 100, date=add_days(today(), -2))
		_so(self.cust, 250.5)
		_so(self.cust, 999, docstatus=0)  # draft
		_si(self.cust, 40)
		_si(self.cust, 0)  # paid
		_si(self.cust, 70, docstatus=0)  # draft invoice
		conv = self._linked("Customer", self.cust, phone="+380501112233")
		frappe.set_user(self.sales)
		card = api.get_card(conv)
		self.assertEqual(card["party_type"], "Customer")
		self.assertEqual(card["party_name"], "Card Customer")
		self.assertEqual(card["link_source"], "manual")
		self.assertEqual(card["contact"]["phone"], "+380501112233")
		self.assertEqual(card["stats"], {"orders_count": 2, "orders_total": 350.5, "currency": "USD"})
		self.assertEqual(len(card["recent_orders"]), 2)
		self.assertEqual(card["recent_orders"][0]["grand_total"], 250.5)
		self.assertEqual([i["outstanding_amount"] for i in card["unpaid_invoices"]], [40])
		self.assertEqual(card["has_access"], {"orders": True, "invoices": True})
		self.assertEqual(card["suggestions"], [])

	def test_recent_orders_capped_at_five(self):
		for i in range(7):
			_so(self.cust, 10 + i)
		conv = self._linked("Customer", self.cust)
		card = api.get_card(conv)  # Administrator
		self.assertEqual(len(card["recent_orders"]), 5)
		self.assertEqual(card["stats"]["orders_count"], 7)

	def test_lead_card(self):
		lead = _lead("Card Lead")
		conv = self._linked("Lead", lead)
		frappe.set_user(self.sales)
		card = api.get_card(conv)
		self.assertEqual(card["party_type"], "Lead")
		self.assertEqual(card["lead"]["name"], lead)
		self.assertEqual(card["lead"]["company_name"], "Acme")
		self.assertTrue(card["lead"]["status"])
		self.assertIsNone(card["stats"])
		self.assertEqual(card["recent_orders"], [])
		self.assertEqual(card["unpaid_invoices"], [])

	def test_unlinked_card_has_suggestions(self):
		lead = _lead("Card Phone Lead", phone="+380509998877")
		conv = _conv(CH_A, phone="+380509998877")
		frappe.set_user(self.sales)
		card = api.get_card(conv)
		self.assertIsNone(card["party"])
		self.assertIsNone(card["stats"])
		self.assertIn(("Lead", lead), [(s["party_type"], s["party"]) for s in card["suggestions"]])

	def test_user_without_sales_read_gets_no_stats_and_no_error(self):
		_so(self.cust, 100)
		_si(self.cust, 40)
		conv = self._linked("Customer", self.cust)
		frappe.set_user(self.plain)
		card = api.get_card(conv)
		self.assertIsNone(card["stats"])
		self.assertEqual(card["recent_orders"], [])
		self.assertEqual(card["unpaid_invoices"], [])
		self.assertEqual(card["has_access"], {"orders": False, "invoices": False})
		self.assertEqual(card["party_name"], "Card Customer")

	def test_conversation_access_refused(self):
		conv = self._linked("Customer", self.cust)
		frappe.set_user(self.other)
		with self.assertRaises(frappe.PermissionError):
			api.get_card(conv)
		with self.assertRaises(frappe.PermissionError):
			api.search_parties(conv, "Card")

	def test_search_parties(self):
		_lead("Card Searchable")
		conv = _conv(CH_A)
		frappe.set_user(self.sales)
		rows = api.search_parties(conv, "Card")
		kinds = {(r["party_type"]) for r in rows}
		self.assertEqual(kinds, {"Customer", "Lead"})
		self.assertLessEqual(len(rows), 10)
		self.assertEqual(api.search_parties(conv, "x"), [])
		frappe.set_user(self.plain)  # no Customer/Lead read: empty, no exception
		self.assertEqual(api.search_parties(conv, "Card"), [])
