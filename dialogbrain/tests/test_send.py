import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import frappe

from dialogbrain.client import DialogBrainError, DialogBrainTimeout
from dialogbrain.tests.utils import DialogBrainTestCase, load_fixture

OUTGOING = "webhook_message_received_outgoing"
THREAD_ID = "8451249"
SEND = load_fixture("messages_send")["data"]
ECHO = load_fixture(OUTGOING)
LONG_TEXT = SEND["text"]  # 184 chars; echo html carries it in full, echo text is cut at 100


def _user(email, *roles):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	user = frappe.get_doc(
		{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
	)
	user.insert(ignore_permissions=True)
	user.add_roles(*roles)
	return email


def _iso(dt):
	return dt.astimezone(timezone.utc).isoformat()


def _process_echo(**message_overrides):
	from dialogbrain.sync.processor import process_event

	data = copy.deepcopy(ECHO["data"])
	data["message"]["sent_at"] = _iso(datetime.now(timezone.utc))
	data["message"].update(message_overrides)
	process_event(ECHO["event"], data)


def _mock_client(execute=None, upload_file=None):
	client = MagicMock()
	if execute is not None:
		client.execute.side_effect = execute
	if upload_file is not None:
		client.upload_file.side_effect = upload_file
	return client


class TestSend(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for dt in ("DB Message Attachment", "DB Message", "DB Conversation", "DB Channel User", "DB Channel"):
			frappe.db.delete(dt)
		self.agent = _user("send-agent@example.com", "Chat Agent")
		self.other = _user("send-other@example.com", "Chat Agent")
		frappe.get_doc(
			{"doctype": "DB Channel", "account_id": "201", "channel_type": "livechat", "title": "Widget"}
		).insert(ignore_permissions=True)
		self.conv = (
			frappe.get_doc(
				{
					"doctype": "DB Conversation",
					"thread_id": THREAD_ID,
					"thread_ref": ECHO["data"]["thread_id"],
					"channel": "201",
					"status": "Open",
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def tearDown(self):
		frappe.set_user("Administrator")

	# --- helpers --------------------------------------------------------------

	def _send(self, text=LONG_TEXT, client_ref="cref-1", user=None, conversation=None, file_urls=None):
		from dialogbrain.api.inbox import send

		frappe.set_user(user or self.agent)
		try:
			with patch("frappe.enqueue") as enqueue:
				result = send(conversation or self.conv, text, client_ref, file_urls)
		finally:
			frappe.set_user("Administrator")
		self.enqueue = enqueue
		return result

	def _deliver(self, name, client):
		from dialogbrain.sync.sender import deliver

		with patch("dialogbrain.sync.sender.DialogBrainClient.from_settings", return_value=client), patch(
			"frappe.enqueue"
		) as enqueue:
			deliver(name)
		self.deliver_enqueue = enqueue

	def _rows(self):
		return frappe.get_all(
			"DB Message",
			filters={"conversation": self.conv},
			fields=["name", "db_id", "message_id", "sent_by", "delivery_status", "author_type", "error", "text"],
		)

	# --- send -----------------------------------------------------------------

	def test_send_creates_pending_row(self):
		result = self._send()
		msg = frappe.get_doc("DB Message", result["name"])
		self.assertEqual(result["delivery_status"], "pending")
		self.assertEqual(
			(msg.direction, msg.author_type, msg.sent_by, msg.delivery_status, msg.client_ref, msg.text),
			("out", "manager", self.agent, "pending", "cref-1", LONG_TEXT),
		)
		self.assertIsNone(msg.db_id)
		self.assertIsNotNone(msg.sent_at)
		self.enqueue.assert_called_once()
		args, kwargs = self.enqueue.call_args
		self.assertEqual(args[0], "dialogbrain.sync.sender.deliver")
		self.assertEqual(kwargs["queue"], "short")
		self.assertEqual(kwargs["message_name"], msg.name)
		self.assertTrue(kwargs.get("enqueue_after_commit"))

	def test_send_idempotent_client_ref(self):
		first = self._send()
		enqueue_first = self.enqueue
		second = self._send()
		self.assertEqual(first["name"], second["name"])
		self.assertEqual(len(self._rows()), 1)
		self.assertEqual(enqueue_first.call_count, 1)
		self.enqueue.assert_not_called()

	def test_send_client_ref_of_other_user_rejected(self):
		self._send()
		with self.assertRaises(frappe.ValidationError):
			self._send(user=self.other)
		self.assertEqual(len(self._rows()), 1)

	def test_send_requires_text_or_files(self):
		with self.assertRaises(frappe.ValidationError):
			self._send(text="   ")
		self.assertEqual(self._rows(), [])

	def test_send_auto_assigns(self):
		self._send()
		self.assertEqual(frappe.db.get_value("DB Conversation", self.conv, "assigned_to"), self.agent)
		# an assigned conversation keeps its assignee
		self._send(user=self.other, client_ref="cref-2")
		self.assertEqual(frappe.db.get_value("DB Conversation", self.conv, "assigned_to"), self.agent)

	def test_send_forbidden_for_restricted_channel(self):
		channel = frappe.get_doc("DB Channel", "201")
		channel.append("allowed_users", {"user": self.other})
		channel.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			self._send()
		self.assertEqual(self._rows(), [])
		self.assertFalse(frappe.db.get_value("DB Conversation", self.conv, "assigned_to"))

	def test_send_rejects_unreadable_file(self):
		private = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "secret.txt",
				"content": b"secret",
				"is_private": 1,
			}
		).insert(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			self._send(file_urls=[private.file_url])
		self.assertEqual(self._rows(), [])

	# --- deliver --------------------------------------------------------------

	def test_deliver_success_sets_db_id(self):
		name = self._send()["name"]
		client = _mock_client(execute=lambda tool_id, params, thread_id=None: copy.deepcopy(SEND))
		self._deliver(name, client)
		client.execute.assert_called_once_with("messages.send", {"thread_id": THREAD_ID, "text": LONG_TEXT})
		msg = frappe.get_doc("DB Message", name)
		self.assertEqual(
			(msg.db_id, msg.message_id, msg.delivery_status, msg.sent_by),
			("27771418", "livechat:msg_65655640abdc", "sent", self.agent),
		)
		self.assertFalse(msg.error)
		conv = frappe.get_doc("DB Conversation", self.conv)
		self.assertEqual(conv.last_message_direction, "out")

	def test_deliver_by_ref_when_thread_id_unknown(self):
		frappe.db.set_value("DB Conversation", self.conv, "thread_id", None)
		name = self._send()["name"]
		client = _mock_client(execute=lambda tool_id, params, thread_id=None: copy.deepcopy(SEND))
		self._deliver(name, client)
		client.execute.assert_called_once_with(
			"messages.send",
			{"thread_id": ECHO["data"]["thread_id"], "text": LONG_TEXT, "from_account_id": 201},
		)
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "sent")

	def test_deliver_record_failure_still_marks_sent(self):
		name = self._send()["name"]
		with patch("dialogbrain.sync.sender._record_sent", side_effect=frappe.TimestampMismatchError):
			self._deliver(name, _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND)))
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "sent")
		_process_echo()  # the echo fills db_id
		rows = self._rows()
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0].name, rows[0].db_id), (name, "27771418"))

	def test_deliver_skips_non_pending(self):
		name = self._send()["name"]
		frappe.db.set_value("DB Message", name, "delivery_status", "unknown")
		client = _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND))
		self._deliver(name, client)
		client.execute.assert_not_called()

	def test_echo_arrives_before_response_merged(self):
		name = self._send()["name"]

		def execute(tool_id, params, thread_id=None):
			_process_echo()  # webhook processed while messages.send is in flight
			return copy.deepcopy(SEND)

		self._deliver(name, _mock_client(execute=execute))
		rows = self._rows()
		self.assertEqual(len(rows), 1)
		self.assertEqual(
			(rows[0].name, rows[0].db_id, rows[0].sent_by, rows[0].delivery_status, rows[0].message_id),
			(name, "27771418", self.agent, "sent", "livechat:msg_65655640abdc"),
		)

	def test_echo_unmatched_row_merged_into_reply(self):
		# The echo could not be matched (e.g. outside the window) and became its own row:
		# the send response's db_message_id folds it into the ERPNext row.
		name = self._send()["name"]

		def execute(tool_id, params, thread_id=None):
			_process_echo(sent_at=_iso(datetime.now(timezone.utc) + timedelta(minutes=10)))
			self.assertEqual(len(self._rows()), 2)
			return copy.deepcopy(SEND)

		self._deliver(name, _mock_client(execute=execute))
		rows = self._rows()
		self.assertEqual(len(rows), 1)
		self.assertEqual(
			(rows[0].name, rows[0].db_id, rows[0].sent_by, rows[0].author_type, rows[0].delivery_status),
			(name, "27771418", self.agent, "manager", "sent"),
		)

	def test_echo_after_response_matched_by_db_id(self):
		name = self._send()["name"]
		self._deliver(name, _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND)))
		_process_echo(sent_at=_iso(datetime.now(timezone.utc) + timedelta(minutes=10)))  # window irrelevant
		rows = self._rows()
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0].name, rows[0].db_id, rows[0].sent_by), (name, "27771418", self.agent))

	def test_long_text_echo_matched(self):
		text = ("Long reply " * 30)[:250]
		self.assertEqual(len(text), 250)
		name = self._send(text=text)["name"]

		def timeout(*a, **k):
			raise DialogBrainTimeout("read timeout")

		self._deliver(name, _mock_client(execute=timeout))
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "unknown")
		_process_echo(html=text, text=text[:100])
		rows = self._rows()
		self.assertEqual(len(rows), 1)
		self.assertEqual(
			(rows[0].name, rows[0].db_id, rows[0].delivery_status, rows[0].text, rows[0].sent_by),
			(name, "27771418", "sent", text, self.agent),
		)

	def test_deliver_timeout_marks_unknown_no_retry(self):
		name = self._send()["name"]
		client = _mock_client(execute=lambda *a, **k: (_ for _ in ()).throw(DialogBrainTimeout("read timeout")))
		self._deliver(name, client)
		msg = frappe.get_doc("DB Message", name)
		self.assertEqual(msg.delivery_status, "unknown")
		self.assertIsNone(msg.db_id)
		self.assertEqual(client.execute.call_count, 1)
		self.deliver_enqueue.assert_not_called()
		from dialogbrain.api.inbox import retry

		with self.assertRaises(frappe.ValidationError):
			retry(name)

	def test_deliver_gateway_timeout_marks_unknown(self):
		name = self._send()["name"]
		client = _mock_client(
			execute=lambda *a, **k: (_ for _ in ()).throw(DialogBrainError("HTTP 504", status=504))
		)
		self._deliver(name, client)
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "unknown")

	def test_deliver_error_marks_failed(self):
		name = self._send()["name"]
		client = _mock_client(
			execute=lambda *a, **k: (_ for _ in ()).throw(DialogBrainError("thread not found", code="NOT_FOUND"))
		)
		self._deliver(name, client)
		msg = frappe.get_doc("DB Message", name)
		self.assertEqual(msg.delivery_status, "failed")
		self.assertIn("thread not found", msg.error)
		self.assertIsNone(msg.db_id)

	def test_deliver_upload_failure_marks_failed_without_send(self):
		file_doc = frappe.get_doc(
			{"doctype": "File", "file_name": "offer.txt", "content": b"offer", "is_private": 0}
		).insert(ignore_permissions=True)
		name = self._send(file_urls=[file_doc.file_url])["name"]
		client = _mock_client(
			execute=lambda *a, **k: copy.deepcopy(SEND),
			upload_file=lambda *a, **k: (_ for _ in ()).throw(DialogBrainTimeout("upload timeout")),
		)
		self._deliver(name, client)
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "failed")
		client.execute.assert_not_called()

	def test_deliver_uploads_files_and_sends_attachments(self):
		file_doc = frappe.get_doc(
			{"doctype": "File", "file_name": "offer.txt", "content": b"offer", "is_private": 0}
		).insert(ignore_permissions=True)
		# the browser posts file_urls as a JSON string
		name = self._send(file_urls=json.dumps([file_doc.file_url]))["name"]
		client = _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND), upload_file=lambda *a, **k: 4242)
		self._deliver(name, client)
		args, _ = client.upload_file.call_args
		self.assertEqual(args[0], "offer.txt")
		self.assertEqual(args[1], b"offer")
		self.assertEqual(args[2], "text/plain")
		client.execute.assert_called_once_with(
			"messages.send", {"thread_id": THREAD_ID, "text": LONG_TEXT, "attachments": [4242]}
		)
		msg = frappe.get_doc("DB Message", name)
		self.assertEqual(msg.delivery_status, "sent")
		self.assertEqual([(a.file_id, a.filename, a.mime_type) for a in msg.attachments], [("4242", "offer.txt", "text/plain")])

	# --- retry ----------------------------------------------------------------

	def test_retry_only_failed(self):
		from dialogbrain.api.inbox import retry

		name = self._send()["name"]
		frappe.set_user(self.agent)
		try:
			for status in ("pending", "sent", "unknown"):
				frappe.db.set_value("DB Message", name, "delivery_status", status)
				with self.assertRaises(frappe.ValidationError):
					retry(name)
			frappe.db.set_value("DB Message", name, {"delivery_status": "failed", "error": "boom"})
			with patch("frappe.enqueue") as enqueue:
				retry(name)
		finally:
			frappe.set_user("Administrator")
		msg = frappe.get_doc("DB Message", name)
		self.assertEqual(msg.delivery_status, "pending")
		self.assertFalse(msg.error)
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["message_name"], name)

	def test_retry_forbidden_for_restricted_channel(self):
		from dialogbrain.api.inbox import retry

		name = self._send()["name"]
		frappe.db.set_value("DB Message", name, "delivery_status", "failed")
		channel = frappe.get_doc("DB Channel", "201")
		channel.append("allowed_users", {"user": self.agent})
		channel.save(ignore_permissions=True)
		frappe.set_user(self.other)
		try:
			with self.assertRaises(frappe.PermissionError):
				retry(name)
		finally:
			frappe.set_user("Administrator")
