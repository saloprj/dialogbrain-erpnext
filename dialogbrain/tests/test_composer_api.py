from unittest.mock import MagicMock, patch

import frappe

from dialogbrain.api import composer as api
from dialogbrain.client import DialogBrainError
from dialogbrain.tests.test_inbox_api import _channel, _conv, _user
from dialogbrain.tests.utils import DialogBrainTestCase

CH_A = "comp-a"
CH_B = "comp-b"
PUBLISH = "dialogbrain.api.composer.publish"
CLIENT = "dialogbrain.api.composer.DialogBrainClient"


def _draft(conversation, draft_id="101", text="Hello there", status="pending"):
	return (
		frappe.get_doc(
			{"doctype": "DB Draft", "draft_id": draft_id, "conversation": conversation,
			 "text": text, "status": status}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _reply(user, **fields):
	frappe.set_user(user)
	doc = {"doctype": "DB Quick Reply", "title": "T", "text": "x"}
	doc.update(fields)
	try:
		return frappe.get_doc(doc).insert().name
	finally:
		frappe.set_user("Administrator")


def _company(name, abbr):
	"""Bare Company row (db_insert skips ERPNext's heavy chart/warehouse setup); only the
	Link target is needed here."""
	if not frappe.db.exists("Company", name):
		doc = frappe.get_doc(
			{"doctype": "Company", "name": name, "company_name": name, "abbr": abbr,
			 "default_currency": "USD", "country": "United States"}
		)
		doc.db_insert()
	return name


class TestComposerApi(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for dt in ("DB Note", "DB Quick Reply", "DB Draft", "DB Message", "DB Conversation",
				   "DB Channel User", "DB Channel"):
			frappe.db.delete(dt)
		self.agent_a = _user("comp-agent-a@example.com", "Chat Agent")
		self.agent_b = _user("comp-agent-b@example.com", "Chat Agent")
		self.mgr = _user("comp-mgr@example.com", "Chat Manager")
		_channel(CH_A, "telegram", [self.agent_a])
		_channel(CH_B, "whatsapp", [self.agent_b])
		self.conv_a = _conv(CH_A, title="Alice", contact_name="Alice Contact")
		self.conv_b = _conv(CH_B, title="Bob")

	def tearDown(self):
		frappe.set_user("Administrator")

	# --- notes ---------------------------------------------------------------

	def test_note_add_list_delete(self):
		frappe.set_user(self.agent_a)
		with patch(PUBLISH) as pub:
			note = api.add_note(self.conv_a, "call back at 5")
		pub.assert_called_once()
		self.assertEqual(pub.call_args[0][:2], (self.conv_a, "conversation"))
		rows = api.list_notes(self.conv_a)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["text"], "call back at 5")
		self.assertEqual(rows[0]["owner"], self.agent_a)
		self.assertTrue(rows[0]["owner_name"])
		api.delete_note(note["name"] if isinstance(note, dict) else note)
		self.assertEqual(api.list_notes(self.conv_a), [])

	def test_note_empty_refused(self):
		frappe.set_user(self.agent_a)
		with self.assertRaises(frappe.ValidationError):
			api.add_note(self.conv_a, "   ")

	def test_note_foreign_channel_refused(self):
		frappe.set_user(self.agent_a)
		with self.assertRaises(frappe.PermissionError):
			api.add_note(self.conv_b, "x")
		with self.assertRaises(frappe.PermissionError):
			api.list_notes(self.conv_b)
		frappe.set_user("Administrator")
		note = api.add_note(self.conv_b, "secret")
		name = note["name"] if isinstance(note, dict) else note
		frappe.set_user(self.agent_a)
		with self.assertRaises(frappe.PermissionError):
			api.delete_note(name)

	def test_note_delete_owner_or_manager_only(self):
		frappe.set_user(self.agent_a)
		note = api.add_note(self.conv_a, "mine")
		name = note["name"] if isinstance(note, dict) else note
		frappe.set_user(self.mgr)
		api.add_note(self.conv_a, "mgr note")
		# another agent on the same channel cannot delete it
		other = _user("comp-agent-a2@example.com", "Chat Agent")
		frappe.set_user("Administrator")
		ch = frappe.get_doc("DB Channel", CH_A)
		ch.append("allowed_users", {"user": other})
		ch.save(ignore_permissions=True)
		frappe.set_user(other)
		with self.assertRaises(frappe.PermissionError):
			api.delete_note(name)
		frappe.set_user(self.mgr)
		api.delete_note(name)
		self.assertFalse(frappe.db.exists("DB Note", name))

	# --- quick replies -----------------------------------------------------------

	def test_quick_replies_visibility(self):
		own = _reply(self.agent_a, title="Own", shortcut="/own")
		_reply(self.agent_b, title="Theirs", shortcut="/theirs")
		shared = _reply(self.mgr, title="Shared", shortcut="/sh", shared=1)
		frappe.set_user(self.agent_a)
		names = {r["name"] for r in api.list_quick_replies(self.conv_a)}
		self.assertEqual(names, {own, shared})
		row = next(r for r in api.list_quick_replies(self.conv_a) if r["name"] == own)
		self.assertEqual(set(row), {"name", "title", "shortcut", "text"})

	def test_quick_reply_placeholders(self):
		company = _company("Comp Main Co", "CMC")
		frappe.db.set_value("DB Channel", CH_A, "company", company)
		customer = frappe.get_doc(
			{"doctype": "Customer", "customer_name": "Zed Corp", "customer_type": "Individual",
			 "customer_group": frappe.db.get_value("Customer Group", {"is_group": 0})}
		).insert(ignore_permissions=True)
		_reply(self.agent_a, title="Hi", text="Hi {customer_name}, {manager_name} from {company}")
		frappe.set_user(self.agent_a)
		fn = frappe.db.get_value("User", self.agent_a, "full_name")
		text = api.list_quick_replies(self.conv_a)[0]["text"]
		self.assertEqual(text, f"Hi Alice Contact, {fn} from {company}")
		frappe.set_user("Administrator")
		frappe.db.set_value("DB Conversation", self.conv_a, {"party_type": "Customer", "party": customer.name})
		frappe.set_user(self.agent_a)
		text = api.list_quick_replies(self.conv_a)[0]["text"]
		self.assertEqual(text, f"Hi Zed Corp, {fn} from {company}")

	def test_quick_reply_company_filter(self):
		c1 = _company("Comp Main Co", "CMC")
		other = _company("Comp Other Co", "COC")
		frappe.db.set_value("DB Channel", CH_A, "company", c1)
		any_ = _reply(self.agent_a, title="Any")
		mine = _reply(self.agent_a, title="Mine", company=c1)
		_reply(self.agent_a, title="Other", company=other)
		frappe.set_user(self.agent_a)
		names = {r["name"] for r in api.list_quick_replies(self.conv_a)}
		self.assertEqual(names, {any_, mine})

	def test_quick_replies_foreign_channel_refused(self):
		frappe.set_user(self.agent_a)
		with self.assertRaises(frappe.PermissionError):
			api.list_quick_replies(self.conv_b)

	def test_agent_cannot_share_quick_reply(self):
		frappe.set_user(self.agent_a)
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc({"doctype": "DB Quick Reply", "title": "x", "text": "y", "shared": 1}).insert()
		frappe.set_user(self.mgr)
		frappe.get_doc({"doctype": "DB Quick Reply", "title": "x", "text": "y", "shared": 1}).insert()

	def test_shortcut_unique_per_owner(self):
		_reply(self.agent_a, shortcut="/dup")
		with self.assertRaises(frappe.ValidationError):
			_reply(self.agent_a, shortcut="/dup")
		_reply(self.agent_b, shortcut="/dup")  # another owner: fine

	def test_agent_cannot_edit_shared_of_manager(self):
		shared = _reply(self.mgr, title="S", shared=1)
		frappe.set_user(self.agent_a)
		self.assertTrue(frappe.has_permission("DB Quick Reply", "read", shared))
		self.assertFalse(frappe.has_permission("DB Quick Reply", "write", shared))
		self.assertFalse(frappe.has_permission("DB Quick Reply", "delete", shared))

	def test_agent_cannot_read_other_private_reply(self):
		theirs = _reply(self.agent_b, title="P")
		frappe.set_user(self.agent_a)
		self.assertFalse(frappe.has_permission("DB Quick Reply", "read", theirs))

	# --- drafts ------------------------------------------------------------------

	def _approve(self, draft, text=None, result=None, error=None):
		client = MagicMock()
		if error:
			client.execute.side_effect = error
		else:
			client.execute.return_value = result or {"status": "sent", "sent_message_id": 9}
		with patch(CLIENT) as cls, patch(PUBLISH) as pub:
			cls.from_settings.return_value = client
			if text is None:
				out = api.approve_draft(draft)
			else:
				out = api.approve_draft(draft, text)
		return client, pub, out

	def test_approve_as_is(self):
		d = _draft(self.conv_a)
		frappe.set_user(self.agent_a)
		client, pub, _ = self._approve(d)
		client.execute.assert_called_once_with("agents.approve_draft", {"draft_id": 101})
		self.assertEqual(frappe.db.get_value("DB Draft", d, "status"), "approved")
		self.assertEqual(pub.call_args[0][:2], (self.conv_a, "draft"))

	def test_approve_edited(self):
		d = _draft(self.conv_a)
		frappe.set_user(self.agent_a)
		client, _, _ = self._approve(d, "Edited text")
		client.execute.assert_called_once_with(
			"agents.approve_draft", {"draft_id": 101, "edited_text": "Edited text"}
		)

	def test_approve_unchanged_text_omits_edit(self):
		d = _draft(self.conv_a)
		frappe.set_user(self.agent_a)
		client, _, _ = self._approve(d, "Hello there")
		client.execute.assert_called_once_with("agents.approve_draft", {"draft_id": 101})

	def test_approve_error_keeps_pending(self):
		d = _draft(self.conv_a)
		frappe.set_user(self.agent_a)
		with self.assertRaises(frappe.ValidationError) as cm:
			self._approve(d, error=DialogBrainError("boom db_live_secret1234"))
		self.assertNotIn("db_live_secret1234", str(cm.exception))
		self.assertEqual(frappe.db.get_value("DB Draft", d, "status"), "pending")

	def test_approve_not_pending_refused(self):
		d = _draft(self.conv_a, status="rejected")
		frappe.set_user(self.agent_a)
		with self.assertRaises(frappe.ValidationError):
			self._approve(d)

	def test_approve_expired_marks_superseded(self):
		d = _draft(self.conv_a)
		frappe.set_user(self.agent_a)
		with self.assertRaises(frappe.ValidationError):
			self._approve(d, result={"status": "expired"})
		self.assertEqual(frappe.db.get_value("DB Draft", d, "status"), "superseded")

	def test_truncated_edit_refused_as_is_allowed(self):
		d = _draft(self.conv_a, text="x" * 500)
		frappe.set_user(self.agent_a)
		with patch(CLIENT) as cls:
			with self.assertRaises(frappe.ValidationError):
				api.approve_draft(d, "my edit")
			cls.from_settings.assert_not_called()
		self.assertEqual(frappe.db.get_value("DB Draft", d, "status"), "pending")
		client, _, _ = self._approve(d)
		client.execute.assert_called_once_with("agents.approve_draft", {"draft_id": 101})
		self.assertEqual(frappe.db.get_value("DB Draft", d, "status"), "approved")

	def test_reject(self):
		d = _draft(self.conv_a)
		frappe.set_user(self.agent_a)
		client = MagicMock()
		with patch(CLIENT) as cls, patch(PUBLISH) as pub:
			cls.from_settings.return_value = client
			api.reject_draft(d)
		client.execute.assert_called_once_with("agents.reject_draft", {"draft_id": 101})
		self.assertEqual(frappe.db.get_value("DB Draft", d, "status"), "rejected")
		self.assertEqual(pub.call_args[0][:2], (self.conv_a, "draft"))

	def test_reject_error_keeps_pending(self):
		d = _draft(self.conv_a)
		frappe.set_user(self.agent_a)
		client = MagicMock()
		client.execute.side_effect = DialogBrainError("nope")
		with patch(CLIENT) as cls, patch(PUBLISH):
			cls.from_settings.return_value = client
			with self.assertRaises(frappe.ValidationError):
				api.reject_draft(d)
		self.assertEqual(frappe.db.get_value("DB Draft", d, "status"), "pending")

	def test_foreign_draft_refused(self):
		d = _draft(self.conv_b, draft_id="202")
		frappe.set_user(self.agent_a)
		with patch(CLIENT) as cls:
			with self.assertRaises(frappe.PermissionError):
				api.approve_draft(d)
			with self.assertRaises(frappe.PermissionError):
				api.reject_draft(d)
			cls.from_settings.assert_not_called()
		self.assertEqual(frappe.db.get_value("DB Draft", d, "status"), "pending")
