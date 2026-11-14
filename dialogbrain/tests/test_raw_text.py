"""Message, draft and note text is stored exactly as written (review F6).

Frappe's XSS filter rewrote a manager's ``Write to Ivan <ivan@example.com> & co`` on save
(v15: entities, v16: the address dropped) and ``deliver`` sent the rewritten text. The
text fields are now stored raw; every place that renders them escapes or sanitises.
"""

import copy
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe

from dialogbrain.tests.utils import DialogBrainTestCase, load_fixture

TYPED = "Write to Ivan <ivan@example.com> & co"
SHORT = "<a@b.c> & x"
EVIL = '<script>alert(1)</script><img src=x onerror="alert(2)">hello'
CH = "raw-ch"


def _user(email, *roles):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	frappe.get_doc(
		{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
	).insert(ignore_permissions=True).add_roles(*roles)
	return email


class TestRawText(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for dt in ("DB Note", "DB Draft", "DB Message Attachment", "DB Message", "DB Conversation",
				   "DB Channel User", "DB Channel"):
			frappe.db.delete(dt)
		frappe.db.set_single_value("DialogBrain Settings", "agent_capabilities", None)
		self.agent = _user("raw-agent@example.com", "Chat Agent")
		frappe.get_doc(
			{"doctype": "DB Channel", "account_id": CH, "channel_type": "livechat", "title": "Widget",
			 "allowed_users": [{"user": self.agent}]}
		).insert(ignore_permissions=True)
		self.conv = (
			frappe.get_doc(
				{"doctype": "DB Conversation", "thread_id": "raw-1", "thread_ref": "livechat:raw-1",
				 "channel": CH, "status": "Open"}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def tearDown(self):
		frappe.set_user("Administrator")

	# --- what the manager typed is what is stored and sent ---------------------------

	def test_reply_is_stored_and_sent_exactly_as_typed(self):
		from dialogbrain.api.inbox import send
		from dialogbrain.sync.sender import deliver

		for i, text in enumerate((TYPED, SHORT)):
			frappe.set_user(self.agent)
			with patch("frappe.enqueue"):
				name = send(self.conv, text, f"raw-cref-{i}")["name"]
			frappe.set_user("Administrator")
			self.assertEqual(frappe.db.get_value("DB Message", name, "text"), text)

			client = MagicMock()
			client.last_request_id = None
			client.last_response_parsed = True
			client.execute.return_value = {"message_id": f"m-{i}"}
			with patch("dialogbrain.sync.sender.DialogBrainClient.from_settings", return_value=client), patch(
				"frappe.enqueue"
			):
				deliver(name)
			sends = [c for c in client.execute.call_args_list if c.args[0] == "messages.send"]
			self.assertEqual(len(sends), 1)
			self.assertEqual(sends[0].args[1]["text"], text)

	def test_reply_shows_in_the_inbox_as_typed(self):
		from dialogbrain.api import conversations as api
		from dialogbrain.api.inbox import send

		frappe.set_user(self.agent)
		with patch("frappe.enqueue"):
			name = send(self.conv, TYPED, "raw-show")["name"]
		row = next(r for r in api.get_messages(self.conv)["rows"] if r["name"] == name)
		self.assertEqual(row["text"], TYPED)
		# the bubble renders ``html`` (v-html): the escaped text, not a dropped "tag"
		self.assertEqual(row["html"], "Write to Ivan &lt;ivan@example.com&gt; &amp; co")

	def test_draft_text_is_stored_as_sent_by_dialogbrain(self):
		from dialogbrain.sync.processor import process_event

		process_event(
			"draft.created",
			{"draft_id": 501, "thread_id": "raw-1", "channel_account_id": CH, "channel_type": "livechat",
			 "channel_ref": "livechat:raw-1", "response_text": SHORT, "created_at": "2026-11-13T12:07:00+00:00"},
		)
		self.assertEqual(frappe.db.get_value("DB Draft", {"draft_id": "501"}, "text"), SHORT)

	def test_note_text_is_stored_as_typed(self):
		from dialogbrain.api.composer import add_note

		frappe.set_user(self.agent)
		with patch("dialogbrain.api.composer.publish"):
			note = add_note(self.conv, SHORT)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("DB Note", note["name"], "text"), SHORT)

	# --- inbound markup cannot execute anywhere -------------------------------------

	def test_inbound_markup_is_inert_in_the_inbox(self):
		from dialogbrain.api import conversations as api
		from dialogbrain.sync.processor import process_event

		fixture = load_fixture("webhook_message_received_inbound")
		data = copy.deepcopy(fixture["data"])
		data["message"]["html"] = EVIL
		data["message"]["db_id"] = "raw-evil-1"
		process_event(fixture["event"], data)
		conv = frappe.db.get_value("DB Message", {"db_id": "raw-evil-1"}, "conversation")
		frappe.set_user("Administrator")
		row = next(r for r in api.get_messages(conv)["rows"] if r["name"] == frappe.db.get_value(
			"DB Message", {"db_id": "raw-evil-1"}))
		for value in (row["html"].lower(), row["text"].lower()):
			self.assertNotIn("<script", value)
			self.assertNotIn("onerror", value)
		self.assertIn("hello", row["text"])
		preview = frappe.db.get_value("DB Conversation", conv, "last_message_preview")
		listed = next(r for r in api.list_conversations()["rows"] if r["name"] == conv)
		self.assertNotIn("<", listed["last_message_preview"])
		self.assertTrue(preview)

	def test_desk_views_escape_the_raw_text(self):
		"""Desk form / list / report views format a field with ``frappe.format``: Data and
		Text formatters inject the value as HTML, the Code formatter escapes it. Raw text
		fields must therefore be Code (and are kept out of Frappe's XSS filter)."""
		for doctype, field in (("DB Message", "text"), ("DB Draft", "text"), ("DB Note", "text"),
							   ("DB Conversation", "last_message_preview")):
			df = frappe.get_meta(doctype).get_field(field)
			self.assertEqual(df.fieldtype, "Code", f"{doctype}.{field}")
			self.assertTrue(df.ignore_xss_filter, f"{doctype}.{field}")

	def test_report_escapes_inbound_text_columns(self):
		"""Unanswered Conversations shows title and preview as Data columns: its formatter
		escapes them (the server keeps raw values for exports)."""
		js = (Path(__file__).parents[1] / "dialogbrain" / "report" / "unanswered_conversations"
			  / "unanswered_conversations.js").read_text()
		self.assertIn("formatter", js)
		self.assertIn("escape_html", js)
		for field in ("title", "last_message_preview"):
			self.assertIn(f'"{field}"', js)

	def test_notification_content_is_sanitised(self):
		from dialogbrain.sync.notify import notify

		frappe.db.set_value("DB Conversation", self.conv, {"title": EVIL, "last_message_preview": EVIL})
		with patch("dialogbrain.sync.notify.recipients", return_value=[self.agent]):
			notify(self.conv, "new_message")
		log = frappe.get_all("Notification Log", filters={"document_name": self.conv, "for_user": self.agent},
							 fields=["subject", "email_content"], order_by="creation desc", limit=1)[0]
		for value in (log.subject, log.email_content):
			self.assertNotIn("<script", value.lower())
			self.assertNotIn("onerror", value.lower())

	def test_conversation_name_fields_from_sync_are_sanitised(self):
		"""title / contact_name come from the channel (a person's display name) and are
		shown by desk views as HTML: sync stores what Frappe's XSS filter would."""
		from dialogbrain.sync.upsert import upsert_conversation

		upsert_conversation({"db_id": "raw-1", "title": EVIL, "contact_name": EVIL}, CH)
		row = frappe.db.get_value("DB Conversation", self.conv, ["title", "contact_name"], as_dict=True)
		for value in (row.title, row.contact_name):
			self.assertNotIn("<script", value.lower())
			self.assertNotIn("onerror", value.lower())
		json.dumps(row)  # plain strings
