import frappe
from frappe.exceptions import UniqueValidationError

from dialogbrain.tests.utils import DialogBrainTestCase


def _conv(thread_id, **kw):
	return frappe.get_doc({"doctype": "DB Conversation", "thread_id": thread_id, **kw})


def _msg(conv, **kw):
	return frappe.get_doc({"doctype": "DB Message", "conversation": conv, "direction": "in", **kw})


class TestDoctypes(DialogBrainTestCase):
	def setUp(self):
		frappe.db.delete("DB Message")
		frappe.db.delete("DB Conversation")
		self.conv = _conv("t-1").insert().name

	def tearDown(self):
		frappe.db.rollback()

	def test_thread_id_unique(self):
		with self.assertRaises(UniqueValidationError):
			_conv("t-1").insert()

	def test_thread_id_optional_blank_is_null(self):
		a = _conv("", thread_ref="telegram:1").insert()
		b = _conv(None, thread_ref="telegram:2").insert()
		self.assertIsNone(frappe.db.get_value("DB Conversation", a.name, "thread_id"))
		self.assertIsNone(frappe.db.get_value("DB Conversation", b.name, "thread_id"))

	def test_channel_thread_ref_unique(self):
		for account in ("dt-1", "dt-2"):
			if not frappe.db.exists("DB Channel", account):
				frappe.get_doc({"doctype": "DB Channel", "account_id": account}).insert()
		_conv(None, channel="dt-1", thread_ref="telegram:9").insert()
		_conv(None, channel="dt-2", thread_ref="telegram:9").insert()  # other account: fine
		with self.assertRaises(UniqueValidationError):
			_conv(None, channel="dt-1", thread_ref="telegram:9").insert()

	def test_db_id_unique(self):
		_msg(self.conv, db_id="m1").insert()
		with self.assertRaises(UniqueValidationError):
			_msg(self.conv, db_id="m1").insert()

	def test_client_ref_unique(self):
		_msg(self.conv, client_ref="c1", direction="out").insert()
		with self.assertRaises(UniqueValidationError):
			_msg(self.conv, client_ref="c1", direction="out").insert()

	def test_two_inbound_without_client_ref(self):
		a = _msg(self.conv, db_id="a", client_ref="").insert()
		b = _msg(self.conv, db_id="b").insert()
		for m in (a, b):
			self.assertIsNone(frappe.db.get_value("DB Message", m.name, "client_ref"))

	def test_two_pending_without_db_id(self):
		a = _msg(self.conv, direction="out", client_ref="x", db_id="").insert()
		b = _msg(self.conv, direction="out", client_ref="y").insert()
		for m in (a, b):
			self.assertIsNone(frappe.db.get_value("DB Message", m.name, "db_id"))

	def test_roles_exist(self):
		for role in ("Chat Agent", "Chat Manager"):
			self.assertTrue(frappe.db.exists("Role", role))

	def test_after_install_idempotent(self):
		from dialogbrain.install import after_install

		after_install()
		after_install()
		self.assertEqual(frappe.db.count("Role", {"name": "Chat Agent"}), 1)

	def test_conversation_indexes(self):
		rows = frappe.db.sql("SHOW INDEX FROM `tabDB Conversation`", as_dict=True)
		cols = {}
		for r in rows:
			cols.setdefault(r["Key_name"], []).append((r["Seq_in_index"], r["Column_name"]))
		keys = {tuple(c for _, c in sorted(v)) for v in cols.values()}
		self.assertIn(("status", "last_message_at"), keys)
		self.assertIn(("assigned_to", "status"), keys)
		self.assertIn(("phone",), keys)
		self.assertIn(("email",), keys)
		mrows = frappe.db.sql("SHOW INDEX FROM `tabDB Message`", as_dict=True)
		mk = {}
		for r in mrows:
			mk.setdefault(r["Key_name"], []).append((r["Seq_in_index"], r["Column_name"]))
		self.assertIn(("conversation", "sent_at"), {tuple(c for _, c in sorted(v)) for v in mk.values()})

	def _index_keys(self, table):
		cols = {}
		for r in frappe.db.sql(f"SHOW INDEX FROM `{table}`", as_dict=True):
			cols.setdefault(r["Key_name"], []).append((r["Seq_in_index"], r["Column_name"]))
		return {tuple(c for _, c in sorted(v)) for v in cols.values()}

	def test_party_and_note_indexes(self):
		# the customer card / party lookups filter conversations by (party_type, party)
		self.assertIn(("party_type", "party"), self._index_keys("tabDB Conversation"))
		# notes are listed per conversation
		self.assertIn(("conversation",), self._index_keys("tabDB Note"))
		self.assertEqual(frappe.get_meta("DB Note").get_field("conversation").search_index, 1)
