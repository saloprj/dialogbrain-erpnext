"""Identity and link fields of DB Conversation are written only by the app's own server
code or a System Manager (review F3): a Chat Manager must not link a conversation to any
customer (bypassing ``party.set_party``'s read check, R12) or swap its thread through the
document API (form save, ``frappe.client``, REST)."""

import json

import frappe

from dialogbrain.tests.test_inbox_api import _channel, _conv, _user
from dialogbrain.tests.utils import DialogBrainTestCase

CH = "guard-ch"


def _customer(name):
	if frappe.db.exists("Customer", {"customer_name": name}):
		return frappe.db.get_value("Customer", {"customer_name": name})
	return (
		frappe.get_doc(
			{"doctype": "Customer", "customer_name": name, "customer_type": "Individual",
			 "customer_group": frappe.db.get_value("Customer Group", {"is_group": 0})}
		)
		.insert(ignore_permissions=True)
		.name
	)


class TestConversationGuard(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for dt in ("DB Message", "DB Conversation", "DB Channel User", "DB Channel"):
			frappe.db.delete(dt)
		self.mgr = _user("guard-mgr@example.com", "Chat Manager")
		self.sysman = _user("guard-sysman@example.com", "System Manager", "Chat Manager")
		_channel(CH, "whatsapp", [self.mgr])
		self.conv = _conv(CH, title="Ivan")
		self.victim = _customer("Guard Victim Corp")

	def tearDown(self):
		frappe.set_user("Administrator")

	def _row(self):
		return frappe.db.get_value(
			"DB Conversation", self.conv, ["party_type", "party", "link_source", "thread_id", "status"], as_dict=True
		)

	def test_chat_manager_cannot_link_a_party_through_client_set_value(self):
		frappe.set_user(self.mgr)
		self.assertTrue(frappe.has_permission("DB Conversation", "write", doc=self.conv))
		with self.assertRaises(frappe.PermissionError):
			frappe.client.set_value(
				"DB Conversation", self.conv,
				{"party_type": "Customer", "party": self.victim, "link_source": "manual"},
			)
		frappe.set_user("Administrator")
		self.assertFalse(self._row().party)

	def test_chat_manager_cannot_swap_identity_through_doc_save(self):
		for field, value in (("thread_id", "999999"), ("thread_ref", "whatsapp:1@s.whatsapp.net"),
							 ("dialogbrain_instance", "ffffffffffffffff"), ("link_source", "manual"),
							 ("phone", "+380671234508"), ("db_contact_id", "77")):
			frappe.set_user(self.mgr)
			doc = frappe.get_doc("DB Conversation", self.conv)
			doc.set(field, value)
			with self.assertRaises(frappe.PermissionError, msg=field):
				doc.save()
		frappe.set_user("Administrator")
		self.assertNotEqual(self._row().thread_id, "999999")

	def test_chat_manager_cannot_insert_a_linked_conversation(self):
		frappe.set_user(self.mgr)
		with self.assertRaises(frappe.PermissionError):
			frappe.client.insert(
				{"doctype": "DB Conversation", "channel": CH, "thread_id": "guard-new",
				 "party_type": "Customer", "party": self.victim, "link_source": "manual"}
			)
		frappe.set_user("Administrator")
		self.assertFalse(frappe.db.exists("DB Conversation", {"thread_id": "guard-new"}))

	def test_flags_sent_by_the_client_are_ignored(self):
		frappe.set_user(self.mgr)
		doc = frappe.get_doc("DB Conversation", self.conv).as_dict()
		doc.update({"party_type": "Customer", "party": self.victim, "link_source": "manual",
					"flags": {"dialogbrain_server_write": True}})
		with self.assertRaises(frappe.PermissionError):
			frappe.client.save(json.dumps(doc, default=str))
		with self.assertRaises(frappe.PermissionError):
			frappe.client.insert(
				{"doctype": "DB Conversation", "channel": CH, "thread_id": "guard-new2",
				 "party_type": "Customer", "party": self.victim, "link_source": "manual",
				 "flags": {"dialogbrain_server_write": True}}
			)
		frappe.set_user("Administrator")
		self.assertFalse(self._row().party)

	def test_rest_update_cannot_link_a_party(self):
		from frappe.api import v1

		frappe.set_user(self.mgr)
		frappe.local.form_dict = frappe._dict(
			data=json.dumps({"party_type": "Customer", "party": self.victim, "link_source": "manual",
							 "flags": {"dialogbrain_server_write": True}})
		)
		try:
			with self.assertRaises(frappe.PermissionError):
				v1.update_doc("DB Conversation", self.conv)
		finally:
			frappe.local.form_dict = frappe._dict()
		frappe.set_user("Administrator")
		self.assertFalse(self._row().party)

	def test_chat_manager_still_edits_workflow_fields(self):
		frappe.set_user(self.mgr)
		frappe.client.set_value("DB Conversation", self.conv, "status", "Closed")
		doc = frappe.get_doc("DB Conversation", self.conv)
		doc.status = "Open"
		doc.save()
		frappe.set_user("Administrator")
		self.assertEqual(self._row().status, "Open")

	def test_system_manager_may_correct_a_link(self):
		frappe.set_user(self.sysman)
		frappe.client.set_value(
			"DB Conversation", self.conv, {"party_type": "Customer", "party": self.victim, "link_source": "manual"}
		)
		frappe.set_user("Administrator")
		self.assertEqual(self._row().party, self.victim)

	def test_set_party_with_read_access_still_works_for_a_chat_manager(self):
		from dialogbrain.party import set_party

		frappe.get_doc("User", self.mgr).add_roles("Sales User")
		frappe.set_user(self.mgr)
		set_party(self.conv, "Customer", self.victim)
		frappe.set_user("Administrator")
		self.assertEqual((self._row().party, self._row().link_source), (self.victim, "manual"))

	def test_sync_creates_conversations_as_the_webhook_job_user(self):
		"""The webhook job runs as Guest (the request was a guest one): the app's own
		insert must still work."""
		from dialogbrain.sync.upsert import upsert_conversation

		frappe.set_user("Guest")
		name, created = upsert_conversation(
			{"db_id": "guard-sync-1", "thread_id": "whatsapp:380671234508@s.whatsapp.net", "title": "X"}, CH
		)
		frappe.set_user("Administrator")
		self.assertTrue(created)
		self.assertEqual(frappe.db.get_value("DB Conversation", name, "thread_id"), "guard-sync-1")
