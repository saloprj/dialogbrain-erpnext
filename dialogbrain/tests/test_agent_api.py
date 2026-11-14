"""Tests for dialogbrain.agent_api (Plan 3, B1): agent user, guard, call log, generic tools.

B2 adds the customer-scoped tools (TestCustomerTools), called through real signed
requests.

Shortcut: the test site has no ERPNext setup data, so Company / Warehouse / Item /
Item Price / Bin rows are inserted with ``db_insert`` (as in test_party_card). The tools
read only a handful of columns.
"""

import json
import time
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from dialogbrain import agent_api
from dialogbrain.party import _primary_contact
from dialogbrain.permissions import permitted_users
from dialogbrain.tests.test_inbox_api import _channel, _conv, _user
from dialogbrain.tests.test_party_card import _customer, _lead
from dialogbrain.tests.test_thread_context import (
	make_request,
	set_secret,
	signed_headers,
)
from dialogbrain.tests.utils import DialogBrainTestCase
from dialogbrain.thread_context import Context

SETTINGS = "DialogBrain Settings"
ROLE = "DialogBrain Agent API"
CO = "Agent Test Co"
OTHER_CO = "Agent Other Co"
PRICE_LIST = "Agent Test Selling"
LOG = "DB Agent Call Log"


def _raw(doctype, name=None, **fields):
	doc = frappe.get_doc({"doctype": doctype, "name": name or frappe.generate_hash(length=10), **fields})
	doc.db_insert()
	return doc.name


def _company(name, abbr):
	if not frappe.db.exists("Company", name):
		_raw("Company", name, company_name=name, abbr=abbr, default_currency="USD", country="United States")
	return name


def _warehouse(label, company, abbr):
	name = f"{label} - {abbr}"
	if not frappe.db.exists("Warehouse", name):
		_raw("Warehouse", name, warehouse_name=label, company=company, is_group=0)
	return name


def _item(code, name=None, description=None, disabled=0, is_sales_item=1, is_stock_item=1):
	frappe.db.delete("Item", {"name": code})
	return _raw("Item", code, item_code=code, item_name=name or code, description=description or code,
				disabled=disabled, is_sales_item=is_sales_item, is_stock_item=is_stock_item, stock_uom="Nos")


def _price(item, rate, currency="USD", price_list=PRICE_LIST, **kw):
	return _raw("Item Price", item_code=item, price_list=price_list, price_list_rate=rate, currency=currency,
				selling=1, **kw)


def _bin(item, warehouse, qty):
	return _raw("Bin", item_code=item, warehouse=warehouse, actual_qty=qty)


def _set_settings(**values):
	for field, value in values.items():
		frappe.db.set_single_value(SETTINGS, field, value)


def tc_context(**fields):
	from dialogbrain.thread_context import Context

	values = {"thread_id": "1", "conversation": None, "party_type": None, "party": None, "identified": True}
	values.update(fields)
	return Context(**values)


def _logs(**filters):
	return frappe.get_all(LOG, filters=filters, fields=["*"], order_by="creation asc")


class TestAgentApi(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for dt in ("DB Message", "DB Conversation", "DB Channel User", "DB Channel", LOG):
			frappe.db.delete(dt)
		_set_settings(dialogbrain_instance="aaaaaaaaaaaaaaaa")  # rows from _conv carry it
		frappe.db.delete("Item Price", {"price_list": PRICE_LIST})
		frappe.db.delete("Bin", {"item_code": ["like", "AGT-%"]})
		self.agent = agent_api.ensure_agent_user()
		self.abbr = "ATC"
		_company(CO, self.abbr)
		_company(OTHER_CO, "AOC")
		self.stores = _warehouse("Stores", CO, self.abbr)
		self.shop = _warehouse("Shop", CO, self.abbr)
		self.foreign = _warehouse("Stores", OTHER_CO, "AOC")
		frappe.db.set_single_value("Selling Settings", "selling_price_list", PRICE_LIST)
		_set_settings(default_company=CO, allow_agent_writes=0)

		_item("AGT-LAPTOP-14", "Laptop 14 inch", "Light 14-inch business laptop")
		_price("AGT-LAPTOP-14", 1299.999)
		_bin("AGT-LAPTOP-14", self.stores, 3)
		_bin("AGT-LAPTOP-14", self.shop, 2)
		_bin("AGT-LAPTOP-14", self.foreign, 100)  # another company's stock never counts

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.after_rollback.reset()

	def _as_agent(self):
		frappe.set_user(self.agent)

	# --- agent user and role --------------------------------------------------------

	def test_ensure_agent_user_is_idempotent_and_minimal(self):
		host = frappe.local.site if "." in frappe.local.site else frappe.local.site + ".local"
		self.assertEqual(self.agent, f"dialogbrain-agent@{host}")
		self.assertEqual(agent_api.ensure_agent_user(), self.agent)
		self.assertEqual(frappe.db.count("User", {"name": self.agent}), 1)
		user = frappe.get_doc("User", self.agent)
		self.assertEqual(user.user_type, "System User")
		self.assertEqual({r.role for r in user.roles}, {ROLE})
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "erpnext_agent_user"), self.agent)
		self.assertEqual(frappe.db.get_value("Role", ROLE, "desk_access"), 1)

	def test_ensure_agent_user_restores_a_removed_role(self):
		frappe.get_doc("User", self.agent).remove_roles(ROLE)
		agent_api.ensure_agent_user()
		self.assertIn(ROLE, frappe.get_roles(self.agent))

	def test_agent_role_grants_no_document_access(self):
		"""The agent key reaches data only through the tools: no /api/resource reads of
		customer documents and no Lead / Sales Order inserts that bypass allow_agent_writes."""
		self._as_agent()
		for doctype in ("Sales Order", "Sales Invoice", "Customer", "Company", "Price List", "Warehouse",
						"UOM", "Account", "Item", "Item Price", "Bin", "Lead"):
			self.assertFalse(frappe.has_permission(doctype, "read"), doctype)
		for doctype in ("Lead", "Sales Order"):
			self.assertFalse(frappe.has_permission(doctype, "create"), doctype)

	# --- guard (Review Focus 4) -----------------------------------------------------

	def test_guard_allows_only_the_agent_user_with_the_role(self):
		self._as_agent()
		agent_api.agent_guard()  # no error

		chat_agent = _user("agent-api-desk@example.com", "Chat Agent")
		other_site = _user("dialogbrain-agent@other-site.example.com", ROLE)
		for user in ("Administrator", chat_agent, "Guest", other_site):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError, msg=user):
				agent_api.agent_guard()

	def test_guard_refuses_the_agent_user_without_the_role(self):
		frappe.get_doc("User", self.agent).remove_roles(ROLE)
		self._as_agent()
		with self.assertRaises(frappe.PermissionError):
			agent_api.agent_guard()

	def test_guard_refuses_when_no_agent_user_is_configured(self):
		_set_settings(erpnext_agent_user=None)
		self._as_agent()
		with self.assertRaises(frappe.PermissionError):
			agent_api.agent_guard()

	def test_tools_refuse_other_users_without_filling_the_log(self):
		"""Final wave F6: a refusal is logged only for the agent user or an API-key call;
		any logged-in desk/portal user (or Guest) hitting the URL writes no log row."""
		for user in ("Administrator", _user("agent-api-desk2@example.com", "Chat Agent"), "Guest"):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError):
				agent_api.get_item("laptop")
			with self.assertRaises(frappe.PermissionError):
				agent_api.create_lead("Mallory")
		frappe.set_user("Administrator")
		self.assertFalse(frappe.db.exists("Lead", {"first_name": "Mallory"}))
		self.assertEqual(_logs(), [])

	def test_refusals_of_the_agent_user_or_an_api_key_call_are_logged(self):
		from werkzeug.test import EnvironBuilder
		from werkzeug.wrappers import Request

		frappe.get_doc("User", self.agent).remove_roles(ROLE)
		self._as_agent()  # the agent user that lost its role
		with self.assertRaises(frappe.PermissionError):
			agent_api.get_item("laptop")
		frappe.set_user(_user("agent-api-key@example.com", "Chat Agent"))  # someone else's API key
		builder = EnvironBuilder(path="/api/method/dialogbrain.agent_api.get_item", method="POST",
								 headers={"Authorization": "token abc:def"})
		try:
			with patch.object(frappe.local, "request", Request(builder.get_environ()), create=True):
				with self.assertRaises(frappe.PermissionError):
					agent_api.get_item("laptop")
		finally:
			builder.close()
		frappe.set_user("Administrator")
		logs = _logs()
		self.assertEqual(len(logs), 2)
		self.assertTrue(all(row.error and "PermissionError" in row.error for row in logs))

	def test_call_log_rows_do_not_block_deleting_the_party(self):
		"""Final wave F6: deleting a Lead / Customer is not blocked by its call log rows."""
		lead = frappe.get_doc({"doctype": "Lead", "first_name": "Log Lead"}).insert(ignore_permissions=True).name
		agent_api._log("find_customer", tc_context(party_type="Lead", party=lead), 1)
		frappe.delete_doc("Lead", lead, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("Lead", lead))
		self.assertEqual(len(_logs(party=lead)), 1)  # the history stays

	# --- get_item --------------------------------------------------------------------

	def test_get_item_returns_price_and_company_stock(self):
		self._as_agent()
		out = agent_api.get_item("laptop")
		self.assertEqual(out, {"items": [{
			"item_code": "AGT-LAPTOP-14",
			"item_name": "Laptop 14 inch",
			"price": 1300.0,
			"currency": "USD",
			"stock_qty": 5.0,
			"in_stock": True,
		}]})

	def test_get_item_searches_code_name_and_description(self):
		self._as_agent()
		for query in ("AGT-LAPTOP", "14 inch", "business"):
			self.assertEqual([i["item_code"] for i in agent_api.get_item(query)["items"]], ["AGT-LAPTOP-14"], query)

	def test_get_item_skips_disabled_and_non_sales_items(self):
		_item("AGT-OLD", "Agent old gadget", disabled=1)
		_price("AGT-OLD", 10)
		_item("AGT-PART", "Agent spare gadget", is_sales_item=0)
		self._as_agent()
		self.assertEqual(agent_api.get_item("gadget"), {"items": []})
		self.assertEqual(agent_api.get_item("AGT-OLD"), {"items": []})

	def test_get_item_without_price_or_stock(self):
		_item("AGT-MOUSE", "Agent mouse")
		self._as_agent()
		item = agent_api.get_item("AGT-MOUSE")["items"][0]
		self.assertIsNone(item["price"])
		self.assertIsNone(item["currency"])
		self.assertEqual(item["stock_qty"], 0.0)
		self.assertFalse(item["in_stock"])

	def test_get_item_ignores_customer_specific_and_expired_prices(self):
		_item("AGT-MON", "Agent monitor")
		_price("AGT-MON", 1, customer="Someone")
		_price("AGT-MON", 2, valid_upto=frappe.utils.add_days(frappe.utils.today(), -1))
		_price("AGT-MON", 3, price_list="Other List")
		_price("AGT-MON", 250)
		self._as_agent()
		self.assertEqual(agent_api.get_item("AGT-MON")["items"][0]["price"], 250.0)

	def test_get_item_uses_the_channel_company_from_context(self):
		ch = _channel("agent-ch", "telegram")
		frappe.db.set_value("DB Channel", ch, "company", OTHER_CO)
		conv = _conv(ch)
		ctx = Context(thread_id="inbox-x", conversation=conv, party_type=None, party=None, identified=False)
		self._as_agent()
		with patch.object(agent_api, "_context", return_value=(ctx, None)):
			self.assertEqual(agent_api.get_item("laptop")["items"][0]["stock_qty"], 100.0)

	def test_non_stock_item_is_in_stock(self):
		_item("AGT-SERVICE", "Agent setup service", is_stock_item=0)
		self._as_agent()
		item = agent_api.get_item("AGT-SERVICE")["items"][0]
		self.assertIsNone(item["stock_qty"])
		self.assertTrue(item["in_stock"])

	def test_get_item_limit_is_capped(self):
		for i in range(25):
			_item(f"AGT-CABLE-{i:02d}", f"Agent cable {i}")
		self._as_agent()
		self.assertEqual(len(agent_api.get_item("cable")["items"]), 5)
		self.assertEqual(len(agent_api.get_item("cable", limit="2")["items"]), 2)
		self.assertEqual(len(agent_api.get_item("cable", limit=500)["items"]), agent_api.MAX_LIMIT)

	def test_get_item_writes_a_log_row(self):
		self._as_agent()
		agent_api.get_item("laptop")
		agent_api.get_item("nothing-matches-this")
		frappe.set_user("Administrator")
		logs = _logs()
		self.assertEqual([(r.tool, r.result_count, r.identified, r.error) for r in logs],
						 [("get_item", 1, 0, "no_headers"), ("get_item", 0, 0, "no_headers")])  # R8

	# --- create_lead -----------------------------------------------------------------

	def test_create_lead_refused_while_writes_are_off(self):
		self._as_agent()
		with self.assertRaises(frappe.PermissionError) as cm:
			agent_api.create_lead("Olena", phone="+380671234567")
		self.assertIn("agent writes are disabled", str(cm.exception).lower())
		frappe.set_user("Administrator")
		self.assertFalse(frappe.db.exists("Lead", {"first_name": "Olena"}))
		(log,) = _logs()
		self.assertEqual(log.tool, "create_lead")
		self.assertIn("disabled", log.error)

	def test_create_lead_creates_a_lead(self):
		_set_settings(allow_agent_writes=1)
		self._as_agent()
		out = agent_api.create_lead("Olena Koval", phone="+380671234567", email="olena@example.com",
									note="Wants 3 laptops")
		frappe.set_user("Administrator")
		lead = frappe.get_doc("Lead", out["lead"])
		self.assertEqual(lead.first_name, "Olena Koval")
		self.assertEqual(lead.mobile_no, "+380671234567")
		self.assertEqual(lead.email_id, "olena@example.com")
		self.assertFalse(lead.db_conversation)
		self.assertTrue(frappe.db.exists("Comment", {"reference_doctype": "Lead", "reference_name": lead.name,
													  "content": ["like", "%Wants 3 laptops%"]}))
		(log,) = _logs()
		self.assertEqual((log.tool, log.result_count, log.error), ("create_lead", 1, "no_headers"))

	def test_create_lead_with_context_links_an_unlinked_conversation(self):
		_set_settings(allow_agent_writes=1)
		conv = _conv(_channel("agent-ch2", "livechat"))
		ctx = Context(thread_id="t-1", conversation=conv, party_type=None, party=None, identified=False)
		self._as_agent()
		with patch.object(agent_api, "_context", return_value=(ctx, None)):
			out = agent_api.create_lead("Visitor Petro")
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Lead", out["lead"], "db_conversation"), conv)
		row = frappe.db.get_value(
			"DB Conversation", conv, ["party_type", "party", "link_source", "contact"], as_dict=True
		)
		# Ruling R6: the agent acted on unverified chat claims -> never "manual"
		self.assertEqual((row.party_type, row.party, row.link_source), ("Lead", out["lead"], "auto_unverified"))
		self.assertEqual(row.contact, _primary_contact("Lead", out["lead"]))
		(log,) = _logs()
		self.assertEqual((log.conversation, log.thread_id), (conv, "t-1"))

	def test_create_lead_never_relinks_a_linked_conversation(self):
		_set_settings(allow_agent_writes=1)
		customer = _customer("Agent Linked Customer")
		conv = _conv(_channel("agent-ch3", "livechat"), party_type="Customer", party=customer,
					 link_source="auto_unverified")
		ctx = Context(thread_id="t-2", conversation=conv, party_type="Customer", party=customer, identified=False)
		self._as_agent()
		with patch.object(agent_api, "_context", return_value=(ctx, None)):
			out = agent_api.create_lead("Someone Else")
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("DB Conversation", conv, "party"), customer)
		self.assertEqual(frappe.db.get_value("Lead", out["lead"], "db_conversation"), conv)

	def test_create_lead_returns_the_chats_linked_lead(self):
		_set_settings(allow_agent_writes=1)
		existing = frappe.get_doc({"doctype": "Lead", "first_name": "Existing"}).insert(ignore_permissions=True).name
		conv = _conv(_channel("agent-ch4", "livechat"), party_type="Lead", party=existing, link_source="auto_unverified")
		ctx = Context(thread_id="t-3", conversation=conv, party_type="Lead", party=existing, identified=False)
		before = frappe.db.count("Lead")
		self._as_agent()
		with patch.object(agent_api, "_context", return_value=(ctx, None)):
			out = agent_api.create_lead("Existing Again")
		frappe.set_user("Administrator")
		self.assertEqual(out, {"lead": existing, "existing": True})
		self.assertEqual(frappe.db.count("Lead"), before)

	def test_create_lead_returns_the_lead_created_from_this_chat(self):
		_set_settings(allow_agent_writes=1)
		customer = _customer("Agent Linked Customer")
		conv = _conv(_channel("agent-ch5", "livechat"), party_type="Customer", party=customer, link_source="manual")
		ctx = Context(thread_id="t-4", conversation=conv, party_type="Customer", party=customer, identified=True)
		self._as_agent()
		with patch.object(agent_api, "_context", return_value=(ctx, None)):
			first = agent_api.create_lead("Visitor One")
			second = agent_api.create_lead("Visitor One Again")
		frappe.set_user("Administrator")
		self.assertEqual(second, {"lead": first["lead"], "existing": True})
		self.assertEqual(frappe.db.count("Lead", {"db_conversation": conv}), 1)

	def test_create_lead_duplicate_email_is_generic(self):
		_set_settings(allow_agent_writes=1)
		frappe.db.set_single_value("CRM Settings", "allow_lead_duplication_based_on_emails", 0)
		other = frappe.get_doc({"doctype": "Lead", "first_name": "Private Person",
								"email_id": "taken-agent@example.com"}).insert(ignore_permissions=True).name
		leads, contacts = frappe.db.count("Lead"), frappe.db.count("Contact")
		self._as_agent()
		out = agent_api.create_lead("Impostor", email="taken-agent@example.com")
		self.assertEqual(out, {"lead": None, "existing": True, "message": agent_api.LEAD_EXISTS})
		self.assertNotIn(other, frappe.as_json(frappe.local.message_log or []))
		frappe.set_user("Administrator")
		self.assertEqual((frappe.db.count("Lead"), frappe.db.count("Contact")), (leads, contacts))
		(log,) = _logs()
		self.assertEqual((log.tool, log.result_count, log.error), ("create_lead", 0, "no_headers"))

	# --- call log --------------------------------------------------------------------

	def test_error_log_row_survives_the_request_rollback(self):
		self._as_agent()
		with self.assertRaises(frappe.PermissionError):
			agent_api.create_lead("Rolled Back")
		frappe.set_user("Administrator")
		with patch.object(frappe.db, "commit"):
			frappe.db.rollback()  # what Frappe does after a failed POST
		(log,) = _logs()
		self.assertEqual(log.tool, "create_lead")
		self.assertIn("disabled", log.error)

	def test_a_broken_failure_log_does_not_mask_the_original_error(self):
		self._as_agent()
		with patch.object(agent_api, "_log", side_effect=frappe.db.InternalError("txn broken")):
			with self.assertRaises(frappe.PermissionError):
				agent_api.create_lead("Masked?")

	def test_cleanup_deletes_rows_older_than_30_days(self):
		from dialogbrain.sync.maintenance import cleanup_agent_call_log

		old = frappe.get_doc({"doctype": LOG, "tool": "get_item"}).insert(ignore_permissions=True).name
		new = frappe.get_doc({"doctype": LOG, "tool": "get_item"}).insert(ignore_permissions=True).name
		frappe.db.set_value(LOG, old, "creation", now_datetime() - timedelta(days=31), update_modified=False)
		frappe.db.set_value(LOG, new, "creation", now_datetime() - timedelta(days=29), update_modified=False)
		cleanup_agent_call_log()
		self.assertFalse(frappe.db.exists(LOG, old))
		self.assertTrue(frappe.db.exists(LOG, new))

	def test_cleanup_is_scheduled_daily(self):
		self.assertIn("dialogbrain.sync.maintenance.cleanup_agent_call_log",
					  frappe.get_hooks("scheduler_events")["daily"])

	# --- schema -----------------------------------------------------------------------

	def test_db_conversation_custom_field(self):
		for doctype in ("Sales Order", "Quotation", "Lead"):
			field = frappe.get_meta(doctype).get_field("db_conversation")
			self.assertIsNotNone(field, doctype)
			self.assertEqual((field.fieldtype, field.options, field.read_only), ("Link", "DB Conversation", 1))

	def test_agent_settings_fields_are_server_managed(self):
		from dialogbrain.setup.connect import SERVER_FIELDS

		for field in ("erpnext_agent_user", "integration_id", "thread_context_secret", "agent_capabilities"):
			self.assertIn(field, SERVER_FIELDS)
		self.assertNotIn("allow_agent_writes", SERVER_FIELDS)

		doc = frappe.get_single(SETTINGS)
		doc.erpnext_agent_user = "Administrator"
		doc.integration_id = "forged"
		doc.allow_agent_writes = 1
		doc.save()
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "erpnext_agent_user"), self.agent)
		self.assertNotEqual(frappe.db.get_single_value(SETTINGS, "integration_id"), "forged")
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "allow_agent_writes"), 1)

	def test_agent_role_created_by_install(self):
		from dialogbrain.install import create_roles

		frappe.db.set_value("Role", ROLE, "desk_access", 1)
		create_roles()
		self.assertTrue(frappe.db.exists("Role", ROLE))


# --- customer-scoped tools (B2) ----------------------------------------------------------

ORDER_CO = "Agent Order Co"
ORDER_ABBR = "AOR"
CUSTOMER_TOOLS = ("find_customer", "order_status", "unpaid_invoices", "create_draft_order")
NOT_IDENTIFIED = {"identified": False, "message": "customer not identified"}


def _clear_cached(doctype, name):
	try:
		from frappe.model.document import clear_document_cache
	except ImportError:  # Frappe v15
		clear_document_cache = frappe.clear_document_cache
	clear_document_cache(doctype, name)


def _so(customer, total, docstatus=1, date=None, currency="USD", **kw):
	return _raw("Sales Order", customer=customer, grand_total=total, docstatus=docstatus, currency=currency,
				status="To Deliver and Bill" if docstatus else "Draft", company=CO,
				transaction_date=date or frappe.utils.today(), **kw)


def _si(customer, outstanding, docstatus=1, due=None, currency="USD", grand_total=None):
	return _raw("Sales Invoice", customer=customer, outstanding_amount=outstanding, docstatus=docstatus,
				currency=currency, due_date=due or frappe.utils.today(), posting_date=frappe.utils.today(),
				grand_total=grand_total if grand_total is not None else outstanding, company=CO)


def _order_setup(item_code):
	"""Just enough ERPNext master data for a real Sales Order insert (db_insert rows)."""
	debtors = f"Debtors - {ORDER_ABBR}"
	stores = f"Stores - {ORDER_ABBR}"
	if not frappe.db.exists("Company", ORDER_CO):
		_raw("Company", ORDER_CO, company_name=ORDER_CO, abbr=ORDER_ABBR, default_currency="USD",
			 country="United States", default_receivable_account=debtors)
	if not frappe.db.exists("Account", debtors):
		_raw("Account", debtors, account_name="Debtors", company=ORDER_CO, account_currency="USD",
			 account_type="Receivable", root_type="Asset", is_group=0)
	if not frappe.db.exists("Warehouse", stores):
		_raw("Warehouse", stores, warehouse_name="Stores", company=ORDER_CO, is_group=0)
	if not frappe.db.exists("UOM", "Nos"):
		_raw("UOM", "Nos", uom_name="Nos", enabled=1)
	if not frappe.db.exists("Item Group", "All Item Groups"):
		_raw("Item Group", "All Item Groups", item_group_name="All Item Groups", is_group=1)
	if not frappe.db.exists("Price List", PRICE_LIST):
		_raw("Price List", PRICE_LIST, price_list_name=PRICE_LIST, currency="USD", selling=1, enabled=1)
	if not frappe.db.get_value("Fiscal Year", {"year_start_date": ["<=", frappe.utils.today()],
											   "year_end_date": [">=", frappe.utils.today()]}):
		year = frappe.utils.getdate(frappe.utils.today()).year
		_raw("Fiscal Year", f"AGT-{year}", year=f"AGT-{year}", year_start_date=f"{year}-01-01",
			 year_end_date=f"{year}-12-31")
	frappe.db.set_value("Item", item_code, "item_group", "All Item Groups")
	_raw("Item Default", parent=item_code, parenttype="Item", parentfield="item_defaults", idx=1,
		 company=ORDER_CO, default_warehouse=stores)
	for doctype, name in (("Company", ORDER_CO), ("Item", item_code), ("Price List", PRICE_LIST),
						  ("UOM", "Nos"), ("Warehouse", stores), ("Account", debtors)):
		_clear_cached(doctype, name)


class TestCustomerTools(DialogBrainTestCase):
	"""Customer tools through real signed requests (Review Focus 1-3)."""

	def setUp(self):
		frappe.set_user("Administrator")
		for dt in ("DB Message", "DB Conversation", "DB Channel User", "DB Channel", LOG):
			frappe.db.delete(dt)
		_set_settings(dialogbrain_instance="aaaaaaaaaaaaaaaa")  # rows from _conv carry it
		frappe.db.delete("Sales Order", {"customer": ["like", "Agent %"]})
		frappe.db.delete("Sales Invoice", {"customer": ["like", "Agent %"]})
		frappe.db.delete("Item Price", {"price_list": PRICE_LIST})
		self.agent = agent_api.ensure_agent_user()
		_company(CO, "ATC")
		_set_settings(default_company=CO, allow_agent_writes=0)
		set_secret()
		self.manager = _user("agent-b2-manager@example.com", "Chat Manager")
		self.channel = _channel("agent-b2", "telegram")
		self.customer = _customer("Agent Ivan")
		self.other = _customer("Agent Mallory")
		for customer in (self.customer, self.other):
			frappe.db.set_value("Customer", customer, "default_currency", "USD")
		self.thread = "73001"
		self.conv = _conv(self.channel, thread_id=self.thread, party_type="Customer", party=self.customer,
						  link_source="manual", title="Ivan chat")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.after_rollback.reset()
		if hasattr(frappe.local, "request"):
			del frappe.local.request

	def call(self, tool, thread=None, headers=None, body=None, query=None, **kwargs):
		"""POST ``tool`` the way DialogBrain does: JSON body (httpx spacing), signed for
		``thread``, dispatched through ``frappe.call`` like Frappe's handler. ``query``
		args are put on the URL (unsigned) and merged into the arguments, as Frappe does
		for an empty or non-JSON body."""
		from urllib.parse import parse_qsl

		path = f"/api/method/dialogbrain.agent_api.{tool}"
		raw = json.dumps(kwargs).encode() if body is None else body
		if headers is None:
			headers = signed_headers(thread=thread or self.thread, ts=int(time.time()), path=path, body=raw)
		frappe.local.request = make_request(path=path, body=raw, headers=headers, query=query)
		args = dict(kwargs, **dict(parse_qsl(query or "")))
		frappe.set_user(self.agent)
		try:
			return frappe.call(getattr(agent_api, tool), **frappe._dict(args, cmd=f"dialogbrain.agent_api.{tool}"))
		finally:
			del frappe.local.request
			frappe.set_user("Administrator")

	# --- identified ----------------------------------------------------------------

	def test_find_customer_with_outstanding_totals_per_currency(self):
		_si(self.customer, 40.004)
		_si(self.customer, 10)
		_si(self.customer, 7.556, currency="EUR")
		_si(self.customer, 0)  # paid
		_si(self.customer, 99, docstatus=0)  # draft
		_si(self.other, 1000)  # another customer's
		out = self.call("find_customer")
		self.assertEqual(out, {
			"identified": True,
			"customer_name": "Agent Ivan",
			"party_type": "Customer",
			"outstanding_total": [{"currency": "EUR", "amount": 7.56}, {"currency": "USD", "amount": 50.0}],
		})
		(log,) = _logs()
		self.assertEqual((log.tool, log.identified, log.conversation, log.thread_id, log.party_type, log.party,
						  log.result_count, log.error),
						 ("find_customer", 1, self.conv, self.thread, "Customer", self.customer, 1, None))

	def test_auto_verified_link_is_identified(self):
		frappe.db.set_value("DB Conversation", self.conv, "link_source", "auto_verified")
		self.assertTrue(self.call("find_customer")["identified"])

	def test_order_status_lists_the_last_five_submitted_orders(self):
		today = frappe.utils.today()
		names = [_so(self.customer, 100 + i, date=frappe.utils.add_days(today, -i)) for i in range(7)]
		_so(self.customer, 5, docstatus=0)  # draft
		_so(self.customer, 6, docstatus=2)  # cancelled
		_so(self.other, 7)
		out = self.call("order_status")
		self.assertTrue(out["identified"])
		self.assertEqual([o["name"] for o in out["orders"]], names[:5])
		self.assertEqual(set(out["orders"][0]), {"name", "date", "status", "delivery_date", "per_delivered",
												 "per_billed", "grand_total", "currency"})
		self.assertEqual(out["orders"][0]["date"], today)
		(log,) = _logs()
		self.assertEqual((log.result_count, log.identified), (5, 1))

	def test_order_status_rounds_to_currency_precision(self):
		name = _so(self.customer, 1299.999, per_delivered=33.3333, per_billed=0,
				   delivery_date=frappe.utils.add_days(frappe.utils.today(), 3))
		(order,) = self.call("order_status", order_no=name)["orders"]
		self.assertEqual((order["grand_total"], order["currency"], order["per_delivered"]), (1300.0, "USD", 33.33))
		self.assertEqual(order["delivery_date"], frappe.utils.add_days(frappe.utils.today(), 3))

	def test_order_no_of_another_customer_returns_nothing(self):
		"""Review Focus 3."""
		mine = _so(self.customer, 100)
		theirs = _so(self.other, 200)
		draft = _so(self.customer, 300, docstatus=0)
		self.assertEqual([o["name"] for o in self.call("order_status", order_no=mine)["orders"]], [mine])
		for order_no in (theirs, draft, "SAL-ORD-DOES-NOT-EXIST", ["in", [theirs]]):
			out = self.call("order_status", order_no=order_no)
			self.assertEqual(out, {"identified": True, "orders": []}, order_no)
			self.assertNotIn(theirs, frappe.as_json(out))

	def test_unpaid_invoices(self):
		due = frappe.utils.add_days(frappe.utils.today(), 10)
		first = _si(self.customer, 12.346, due=frappe.utils.today(), grand_total=100.004)
		for _i in range(11):
			_si(self.customer, 1, due=due)
		_si(self.customer, 0)
		_si(self.customer, -5)  # credit note
		_si(self.customer, 9, docstatus=0)
		_si(self.other, 500)
		out = self.call("unpaid_invoices")
		self.assertTrue(out["identified"])
		self.assertEqual(len(out["invoices"]), 10)
		self.assertEqual(out["invoices"][0], {"name": first, "date": frappe.utils.today(),
											  "due_date": frappe.utils.today(), "grand_total": 100.0,
											  "outstanding_amount": 12.35, "currency": "USD"})
		self.assertEqual(out["outstanding_total"], [{"currency": "USD", "amount": 23.35}])  # all 12, not only 10
		(log,) = _logs()
		self.assertEqual((log.result_count, log.identified), (10, 1))

	def test_lead_party_is_identified_without_financials(self):
		lead = _lead("Agent Olena")
		frappe.db.set_value("DB Conversation", self.conv, {"party_type": "Lead", "party": lead})
		self.assertEqual(self.call("find_customer"), {"identified": True, "customer_name": "Agent Olena",
													  "party_type": "Lead", "outstanding_total": []})
		self.assertEqual(self.call("order_status"), {"identified": True, "orders": []})
		self.assertEqual(self.call("unpaid_invoices"),
						 {"identified": True, "invoices": [], "outstanding_total": []})

	# --- not identified (Review Focus 1, 2) -------------------------------------------

	def _assert_not_identified_everywhere(self, label, reason, **call_kwargs):
		_so(self.customer, 100)
		_si(self.customer, 40)
		_set_settings(allow_agent_writes=1)
		frappe.db.delete(LOG)
		for tool in CUSTOMER_TOOLS:
			kwargs = {"items": [{"item_code": "AGT-LAPTOP-14", "qty": 1}]} if tool == "create_draft_order" else {}
			out = self.call(tool, **call_kwargs, **kwargs)
			self.assertEqual(out, NOT_IDENTIFIED, f"{label}: {tool}")
		logs = _logs()
		self.assertEqual([r.tool for r in logs], list(CUSTOMER_TOOLS), label)
		# Ruling R8: the reason code is in the log, never in the answer
		self.assertEqual({(r.identified, r.result_count, r.error) for r in logs}, {(0, 0, reason)}, label)
		self.assertFalse(frappe.db.exists("Sales Order", {"db_conversation": self.conv}), label)
		return logs

	def _headers(self, tool="find_customer", body=b"{}", **kw):
		path = f"/api/method/dialogbrain.agent_api.{tool}"
		return signed_headers(**{"thread": self.thread, "ts": int(time.time()), "path": path, "body": body, **kw})

	def test_bad_signature(self):
		_set_settings(allow_agent_writes=1)
		for tool in CUSTOMER_TOOLS:
			headers = self._headers(tool)
			headers["X-DialogBrain-Context-Signature"] = "sha256=" + "0" * 64
			self.assertEqual(self.call(tool, headers=headers), NOT_IDENTIFIED, tool)
		self.assertEqual({(r.identified, r.conversation, r.error) for r in _logs()}, {(0, None, "bad_signature")})

	def test_signed_with_another_secret(self):
		"""Another workspace's secret (or a forger without the secret)."""
		self.assertEqual(self.call("find_customer", headers=self._headers(secret="e" * 64)), NOT_IDENTIFIED)
		(log,) = _logs()
		self.assertEqual(log.error, "bad_signature")

	def test_no_secret_reason_is_logged(self):
		from frappe.utils.password import remove_encrypted_password

		remove_encrypted_password(SETTINGS, SETTINGS, "thread_context_secret")
		self.assertEqual(self.call("find_customer"), NOT_IDENTIFIED)
		(log,) = _logs()
		self.assertEqual(log.error, "no_secret")

	def test_identified_calls_log_no_reason(self):
		self.call("find_customer")
		(log,) = _logs()
		self.assertEqual((log.identified, log.error), (1, None))

	def test_stale_timestamp(self):
		headers = self._headers(ts=int(time.time()) - 301)
		self.assertEqual(self.call("find_customer", headers=headers), NOT_IDENTIFIED)
		(log,) = _logs()
		self.assertEqual((log.identified, log.conversation, log.error), (0, None, "stale"))

	def test_missing_header(self):
		for header in ("X-DialogBrain-Thread-Id", "X-DialogBrain-Timestamp", "X-DialogBrain-Context-Signature"):
			headers = self._headers()
			del headers[header]
			self.assertEqual(self.call("find_customer", headers=headers), NOT_IDENTIFIED, header)
		self.assertEqual(self.call("find_customer", headers={}), NOT_IDENTIFIED)
		self.assertEqual({r.error for r in _logs()}, {"no_headers"})

	def test_signature_replayed_for_another_chat_or_tool(self):
		other_conv = _conv(self.channel, thread_id="73002", party_type="Customer", party=self.other,
						   link_source="manual")
		headers = self._headers()
		headers["X-DialogBrain-Thread-Id"] = "73002"  # another chat's thread under this signature
		self.assertEqual(self.call("find_customer", headers=headers), NOT_IDENTIFIED)
		# a find_customer signature replayed against order_status
		self.assertEqual(self.call("order_status", headers=self._headers()), NOT_IDENTIFIED)
		# a signed body for one order replayed with another order_no
		body = json.dumps({"order_no": "A"}).encode()
		headers = self._headers("order_status", body=body)
		out = self.call("order_status", headers=headers, body=json.dumps({"order_no": "B"}).encode(),
						order_no="B")
		self.assertEqual(out, NOT_IDENTIFIED)
		self.assertTrue(all(r.identified == 0 for r in _logs()))
		self.assertNotIn(other_conv, [r.conversation for r in _logs()])

	def test_unknown_thread(self):
		self._assert_not_identified_everywhere("unknown thread", "unknown_thread", thread="999999")

	def test_auto_unverified_link_is_not_identified(self):
		"""Review Focus 2: e.g. a phone typed into livechat matched a customer."""
		frappe.db.set_value("DB Conversation", self.conv, "link_source", "auto_unverified")
		logs = self._assert_not_identified_everywhere("auto_unverified", "unverified")
		self.assertTrue(all(r.conversation == self.conv for r in logs))

	def test_unlinked_conversation_is_not_identified(self):
		frappe.db.set_value("DB Conversation", self.conv, {"party_type": None, "party": None, "link_source": None})
		self._assert_not_identified_everywhere("unlinked", "unlinked")

	def test_party_that_no_longer_exists_is_not_identified(self):
		frappe.db.set_value("DB Conversation", self.conv, "party", "Agent Deleted Customer")
		self.assertEqual(self.call("find_customer"), NOT_IDENTIFIED)
		(log,) = _logs()
		self.assertEqual((log.identified, log.error), (0, "unlinked"))

	def test_no_context_at_all(self):
		frappe.set_user(self.agent)
		for tool in ("find_customer", "order_status", "unpaid_invoices"):
			self.assertEqual(getattr(agent_api, tool)(), NOT_IDENTIFIED, tool)

	# --- query strings (Ruling R9) -------------------------------------------------------

	def test_signed_empty_body_replayed_with_a_query_string_is_refused(self):
		"""Frappe merges ``?order_no=`` into the arguments when the body is empty, and the
		signature does not cover it: refuse any query string."""
		theirs = _so(self.other, 200)
		mine = _so(self.customer, 100)
		path = "/api/method/dialogbrain.agent_api.order_status"
		headers = signed_headers(thread=self.thread, ts=int(time.time()), path=path, body=b"")
		self.assertEqual([o["name"] for o in self.call("order_status", headers=headers, body=b"")["orders"]],
						 [mine])
		for order_no in (theirs, mine):
			with self.assertRaises(frappe.ValidationError) as cm:
				self.call("order_status", headers=headers, body=b"", query=f"order_no={order_no}")
			self.assertIn("Query parameters are not accepted", str(cm.exception))
			self.assertNotIn(theirs, frappe.as_json(frappe.local.message_log or []))
		logs = _logs()
		self.assertEqual(len(logs), 3)
		self.assertTrue(all("Query parameters" in r.error for r in logs[1:]))

	def test_every_tool_refuses_a_query_string(self):
		_set_settings(allow_agent_writes=1)
		for tool in ("get_item", "create_lead", *CUSTOMER_TOOLS):
			with self.assertRaises(frappe.ValidationError, msg=tool):
				self.call(tool, query="x=1")
		with self.assertRaises(frappe.ValidationError):
			self.call("get_item", query="query=laptop", headers={}, body=b"")
		frappe.set_user("Administrator")
		self.assertFalse(frappe.db.exists("Lead", {"db_conversation": self.conv}))
		self.assertEqual(len(_logs()), len(CUSTOMER_TOOLS) + 3)

	# --- extra arguments (Review Focus 3) ----------------------------------------------

	def test_customer_arguments_are_refused(self):
		_si(self.other, 1000)
		cases = [("find_customer", {"phone": "+380501112233"}),
				 ("find_customer", {"customer": self.other}),
				 ("order_status", {"order_no": "X", "email": "mallory@example.com"}),
				 ("unpaid_invoices", {"customer": self.other, "phone": "1"}),
				 ("create_draft_order", {"items": [], "customer": self.other})]
		for tool, kwargs in cases:
			with self.assertRaises(frappe.ValidationError, msg=tool) as cm:
				self.call(tool, **kwargs)
			message = str(cm.exception)
			self.assertIn("Unexpected argument(s)", message)
			self.assertIn("identified from the chat", message)
			self.assertNotIn("order_no", message.split(";")[0])
		logs = _logs()
		self.assertEqual(len(logs), len(cases))
		self.assertTrue(all("Unexpected argument" in r.error for r in logs))

	def test_unexpected_argument_message_lists_the_names(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self.call("unpaid_invoices", phone="1", customer="x")
		self.assertIn("Unexpected argument(s): customer, phone;", str(cm.exception))

	def test_frappe_keys_are_accepted(self):
		out = self.call("find_customer", _="123", csrf_token="t")
		self.assertTrue(out["identified"])

	def test_customer_tools_are_guarded(self):
		for user in ("Administrator", "Guest", self.manager):
			frappe.set_user(user)
			for tool in CUSTOMER_TOOLS:
				with self.assertRaises(frappe.PermissionError, msg=f"{user} {tool}"):
					getattr(agent_api, tool)()

	# --- create_draft_order ------------------------------------------------------------

	def _order_ready(self):
		_item("AGT-LAPTOP-14", "Laptop 14 inch")
		_price("AGT-LAPTOP-14", 1299.999)
		_order_setup("AGT-LAPTOP-14")
		frappe.db.set_single_value("Selling Settings", "selling_price_list", PRICE_LIST)
		frappe.db.set_value("DB Channel", self.channel, "company", ORDER_CO)
		_set_settings(allow_agent_writes=1)

	def test_create_draft_order_refused_while_writes_are_off(self):
		"""Ruling R4: enforced server-side, whatever DialogBrain's denied_tools say."""
		self._order_ready()
		_set_settings(allow_agent_writes=0)
		with self.assertRaises(frappe.PermissionError):
			self.call("create_draft_order", items=[{"item_code": "AGT-LAPTOP-14", "qty": 1}])
		self.assertFalse(frappe.db.exists("Sales Order", {"db_conversation": self.conv}))
		(log,) = _logs()
		self.assertIn("disabled", log.error)

	def test_create_draft_order_creates_a_draft_and_notifies_the_assignee(self):
		self._order_ready()
		frappe.db.set_value("DB Conversation", self.conv, "assigned_to", self.manager)
		out = self.call("create_draft_order", items=[{"item_code": "AGT-LAPTOP-14", "qty": 2}])
		self.assertEqual(out["identified"], True)
		self.assertEqual(out["status"], "Draft")
		self.assertEqual(out["currency"], "USD")
		self.assertEqual(out["grand_total"], 2600.0)
		self.assertEqual(out["items"], [{"item_code": "AGT-LAPTOP-14", "item_name": "Laptop 14 inch",
										 "qty": 2.0, "rate": 1300.0}])
		so = frappe.get_doc("Sales Order", out["order"])
		self.assertEqual((so.docstatus, so.customer, so.db_conversation, so.company, so.owner),
						 (0, self.customer, self.conv, ORDER_CO, self.agent))
		self.assertEqual(so.items[0].warehouse, f"Stores - {ORDER_ABBR}")
		notes = frappe.get_all("Notification Log", filters={"document_type": "Sales Order",
															 "document_name": so.name}, pluck="for_user")
		self.assertEqual(notes, [self.manager])
		(log,) = _logs()
		self.assertEqual((log.tool, log.identified, log.result_count, log.error), ("create_draft_order", 1, 1, None))
		self.assertEqual(frappe.session.user, "Administrator")

	def test_create_draft_order_currency_follows_the_party_not_the_site_default(self):
		self._order_ready()
		frappe.db.set_value("Customer", self.customer, "default_currency", None)
		frappe.db.set_default("currency", "INR")  # the site-wide default a new document would get
		out = self.call("create_draft_order", items=[{"item_code": "AGT-LAPTOP-14", "qty": 1}])
		self.assertEqual(out["currency"], "USD")  # the company's currency

	def test_create_draft_order_notifies_the_channel_users_when_unassigned(self):
		self._order_ready()
		agent_user = _user("agent-b2-chat@example.com", "Chat Agent")
		channel = frappe.get_doc("DB Channel", self.channel)
		channel.append("allowed_users", {"user": agent_user})
		channel.save(ignore_permissions=True)
		out = self.call("create_draft_order", items=json.dumps([{"item_code": "AGT-LAPTOP-14", "qty": 1}]))
		notes = frappe.get_all("Notification Log", filters={"document_name": out["order"]}, pluck="for_user")
		self.assertEqual(sorted(notes), permitted_users(self.channel))
		self.assertTrue({self.manager, agent_user} <= set(notes))

	def test_create_draft_order_restores_the_agent_session_on_failure(self):
		self._order_ready()
		frappe.local.request = make_request(path="/api/method/dialogbrain.agent_api.create_draft_order",
											body=b"{}", headers={})
		ctx = agent_api.Context(thread_id=self.thread, conversation=self.conv, party_type="Customer",
								party=self.customer, identified=True)
		frappe.set_user(self.agent)
		with (
			patch.object(agent_api, "_context", return_value=(ctx, None)),
			patch("frappe.model.document.Document.insert", side_effect=frappe.ValidationError("boom")),
			self.assertRaises(frappe.ValidationError),
		):
			agent_api.create_draft_order(items=[{"item_code": "AGT-LAPTOP-14", "qty": 1}])
		self.assertEqual(frappe.session.user, self.agent)

	def test_create_draft_order_validates_items(self):
		self._order_ready()
		_item("AGT-OLD", "Old", disabled=1)
		_item("AGT-PART", "Part", is_sales_item=0)
		bad = [
			[],
			"not json",
			[{"item_code": "AGT-LAPTOP-14", "qty": 0}],
			[{"item_code": "AGT-LAPTOP-14", "qty": 1, "rate": 1}],
			[{"item_code": "AGT-LAPTOP-14", "qty": 1, "warehouse": "x"}],
			[{"item_code": "AGT-OLD", "qty": 1}],
			[{"item_code": "AGT-PART", "qty": 1}],
			[{"item_code": "AGT-NOPE", "qty": 1}],
			[{"qty": 1}],
			["AGT-LAPTOP-14"],
			[{"item_code": "AGT-LAPTOP-14", "qty": 1}] * (agent_api.MAX_ORDER_LINES + 1),
		]
		for items in bad:
			with self.assertRaises(frappe.ValidationError, msg=repr(items)[:60]):
				self.call("create_draft_order", items=items)
		self.assertFalse(frappe.db.exists("Sales Order", {"db_conversation": self.conv}))

	def test_create_draft_order_validates_qty(self):
		self._order_ready()
		bad = [float("nan"), float("inf"), float("-inf"), "nan", "inf", "1e300", 1e300, True, False, -1, "-2",
			   0, "0", agent_api.MAX_QTY + 1, 10**400, None, "", "two", [1], {"n": 1}]
		for qty in bad:
			with self.assertRaises(frappe.ValidationError, msg=repr(qty)[:40]) as cm:
				# the body is built here: json.dumps cannot carry 10**400 as a float
				self.call("create_draft_order", body=b"{}", items=[{"item_code": "AGT-LAPTOP-14", "qty": qty}])
			self.assertIn("Quantity", str(cm.exception), repr(qty)[:40])
		self.assertFalse(frappe.db.exists("Sales Order", {"db_conversation": self.conv}))
		for qty, expected in (("2", 2.0), (1.5, 1.5), (agent_api.MAX_QTY, float(agent_api.MAX_QTY))):
			out = self.call("create_draft_order", items=[{"item_code": "AGT-LAPTOP-14", "qty": qty}])
			self.assertEqual(out["items"][0]["qty"], expected)

	def test_create_draft_order_failure_is_generic_for_the_caller(self):
		"""Ruling R9: ERPNext's message from the elevated insert (account / warehouse names)
		reaches only the call log."""
		from erpnext.selling.doctype.sales_order.sales_order import SalesOrder

		self._order_ready()

		def leaky_validate(doc):
			frappe.throw("Account Debtors - AOR is frozen for Agent Mallory")

		frappe.local.message_log = []
		with patch.object(SalesOrder, "validate", leaky_validate), self.assertRaises(frappe.ValidationError) as cm:
			self.call("create_draft_order", items=[{"item_code": "AGT-LAPTOP-14", "qty": 1}])
		self.assertIsInstance(cm.exception, agent_api.DraftOrderFailed)
		self.assertEqual(str(cm.exception), agent_api.DRAFT_FAILED)
		self.assertIsNone(cm.exception.__cause__)
		self.assertTrue(cm.exception.__suppress_context__)
		self.assertNotIn("Debtors", frappe.as_json(frappe.local.message_log or []))
		(log,) = _logs()
		self.assertIn(agent_api.DRAFT_FAILED, log.error)
		self.assertIn("Account Debtors - AOR is frozen", log.error)

	def test_create_draft_order_drops_messages_from_the_elevated_insert(self):
		from erpnext.selling.doctype.sales_order.sales_order import SalesOrder

		self._order_ready()
		original = SalesOrder.validate

		def chatty_validate(doc):
			frappe.msgprint("internal elevated note: Debtors - AOR")
			original(doc)

		frappe.local.message_log = []
		frappe.msgprint("kept note")
		with patch.object(SalesOrder, "validate", chatty_validate):
			out = self.call("create_draft_order", items=[{"item_code": "AGT-LAPTOP-14", "qty": 1}])
		self.assertTrue(out["order"])
		log = frappe.as_json(frappe.local.message_log)
		self.assertIn("kept note", log)
		self.assertNotIn("internal elevated note", log)

	def test_create_draft_order_for_a_lead(self):
		self._order_ready()
		lead = _lead("Agent Lead Buyer")
		frappe.db.set_value("DB Conversation", self.conv, {"party_type": "Lead", "party": lead})
		out = self.call("create_draft_order", items=[{"item_code": "AGT-LAPTOP-14", "qty": 1}])
		self.assertEqual((out["identified"], out["order"]), (True, None))
		self.assertFalse(frappe.db.exists("Sales Order", {"db_conversation": self.conv}))

	def test_create_draft_order_is_never_submitted(self):
		self._order_ready()
		with patch("erpnext.selling.doctype.sales_order.sales_order.SalesOrder.submit") as submit:
			out = self.call("create_draft_order", items=[{"item_code": "AGT-LAPTOP-14", "qty": 1}])
		submit.assert_not_called()
		self.assertEqual(frappe.db.get_value("Sales Order", out["order"], "docstatus"), 0)
