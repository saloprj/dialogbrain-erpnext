"""Tests for dialogbrain.agent_api (Plan 3, B1): agent user, guard, call log, generic tools.

Shortcut: the test site has no ERPNext setup data, so Company / Warehouse / Item /
Item Price / Bin rows are inserted with ``db_insert`` (as in test_party_card). The tools
read only a handful of columns.
"""

from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from dialogbrain import agent_api
from dialogbrain.tests.test_inbox_api import _channel, _conv, _user
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


def _logs(**filters):
	return frappe.get_all(LOG, filters=filters, fields=["*"], order_by="creation asc")


class TestAgentApi(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for dt in ("DB Message", "DB Conversation", "DB Channel User", "DB Channel", LOG):
			frappe.db.delete(dt)
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

	def test_tools_refuse_other_users_and_log_the_refusal(self):
		for user in ("Administrator", _user("agent-api-desk2@example.com", "Chat Agent"), "Guest"):
			frappe.set_user(user)
			with self.assertRaises(frappe.PermissionError):
				agent_api.get_item("laptop")
			with self.assertRaises(frappe.PermissionError):
				agent_api.create_lead("Mallory")
		frappe.set_user("Administrator")
		self.assertFalse(frappe.db.exists("Lead", {"first_name": "Mallory"}))
		logs = _logs()
		self.assertEqual(len(logs), 6)
		self.assertTrue(all(row.error for row in logs))
		self.assertEqual({row.tool for row in logs}, {"get_item", "create_lead"})

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
		with patch.object(agent_api, "_context", return_value=ctx):
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
						 [("get_item", 1, 0, None), ("get_item", 0, 0, None)])

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
		self.assertEqual((log.tool, log.result_count, log.error), ("create_lead", 1, None))

	def test_create_lead_with_context_links_an_unlinked_conversation(self):
		_set_settings(allow_agent_writes=1)
		conv = _conv(_channel("agent-ch2", "livechat"))
		ctx = Context(thread_id="t-1", conversation=conv, party_type=None, party=None, identified=False)
		self._as_agent()
		with patch.object(agent_api, "_context", return_value=ctx):
			out = agent_api.create_lead("Visitor Petro")
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("Lead", out["lead"], "db_conversation"), conv)
		row = frappe.db.get_value("DB Conversation", conv, ["party_type", "party", "link_source"], as_dict=True)
		self.assertEqual((row.party_type, row.party, row.link_source), ("Lead", out["lead"], "manual"))
		(log,) = _logs()
		self.assertEqual((log.conversation, log.thread_id), (conv, "t-1"))

	def test_create_lead_never_relinks_a_linked_conversation(self):
		_set_settings(allow_agent_writes=1)
		existing = frappe.get_doc({"doctype": "Lead", "first_name": "Existing"}).insert(ignore_permissions=True).name
		conv = _conv(_channel("agent-ch3", "livechat"), party_type="Lead", party=existing, link_source="auto_unverified")
		ctx = Context(thread_id="t-2", conversation=conv, party_type="Lead", party=existing, identified=False)
		self._as_agent()
		with patch.object(agent_api, "_context", return_value=ctx):
			out = agent_api.create_lead("Someone Else")
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("DB Conversation", conv, "party"), existing)
		self.assertEqual(frappe.db.get_value("Lead", out["lead"], "db_conversation"), conv)

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
