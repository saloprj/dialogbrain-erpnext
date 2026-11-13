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


def _mock_client(execute=None, upload_file=None, echo=True):
	"""``echo``: like DialogBrain with Part A1, the client reports the request_id it got
	back (``last_request_id``); False = an older DialogBrain that ignores it."""
	client = MagicMock()
	client.last_request_id = None
	if execute is not None:

		def run(*a, **k):
			client.last_request_id = k.get("request_id") if echo else None
			return execute(*a, **k)

		client.execute.side_effect = run
	if upload_file is not None:
		client.upload_file.side_effect = upload_file
	return client


class TestSend(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for dt in ("DB Message Attachment", "DB Message", "DB Conversation", "DB Channel User", "DB Channel"):
			frappe.db.delete(dt)
		frappe.db.set_single_value("DialogBrain Settings", "agent_capabilities", None)  # Plan 1 baseline
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

	def test_send_auto_assign_publishes_conversation(self):
		# the open conversation's header shows the assignee: it refreshes on a
		# `conversation` event, not on `message` (live e2e, Plan 2 Task 8)
		with patch("dialogbrain.sync.sender.publish") as publish:
			self._send()
		kinds = [c.args[1] for c in publish.call_args_list]
		self.assertIn("conversation", kinds)
		self.assertIn("message", kinds)
		# already assigned: no extra conversation event
		with patch("dialogbrain.sync.sender.publish") as publish:
			self._send(user=self.other, client_ref="cref-2")
		self.assertEqual([c.args[1] for c in publish.call_args_list], ["message"])

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

	# --- never sent twice: claim + conditional outcome ----------------------

	def test_deliver_twice_sends_once(self):
		name = self._send()["name"]
		client = _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND))
		self._deliver(name, client)
		self._deliver(name, client)
		self.assertEqual(client.execute.call_count, 1)

	def test_deliver_claims_and_commits_before_sending(self):
		name = self._send()["name"]
		seen = []

		def execute(*a, **k):
			seen.append(frappe.db.get_value("DB Message", name, "delivery_status"))
			return copy.deepcopy(SEND)

		with patch("dialogbrain.sync.sender._commit") as commit:
			commit.side_effect = lambda: seen.append("commit")
			self._deliver(name, _mock_client(execute=execute))
		self.assertEqual(seen[:2], ["commit", "unknown"])  # claimed and committed before the call

	def test_killed_worker_leaves_claim_and_redeliver_does_not_send(self):
		name = self._send()["name"]

		def killed(*a, **k):
			raise SystemExit  # worker dies mid-request; the claim was committed before

		with self.assertRaises(SystemExit):
			self._deliver(name, _mock_client(execute=killed))
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "unknown")
		client = _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND))
		self._deliver(name, client)  # e.g. a re-enqueued job
		client.execute.assert_not_called()

	def test_echo_during_send_then_error_stays_sent_and_retry_refused(self):
		from dialogbrain.api.inbox import retry

		name = self._send()["name"]

		def execute(*a, **k):
			_process_echo()  # channel delivered, echo committed sent + db_id
			raise DialogBrainError("DialogBrain HTTP 500: boom", status=500)

		self._deliver(name, _mock_client(execute=execute))
		msg = frappe.get_doc("DB Message", name)
		self.assertEqual((msg.delivery_status, msg.db_id), ("sent", "27771418"))
		with self.assertRaises(frappe.ValidationError):
			retry(name)

	def test_echo_during_send_then_timeout_stays_sent(self):
		name = self._send()["name"]

		def execute(*a, **k):
			_process_echo()
			raise DialogBrainTimeout("read timeout")

		self._deliver(name, _mock_client(execute=execute))
		msg = frappe.get_doc("DB Message", name)
		self.assertEqual((msg.delivery_status, msg.db_id), ("sent", "27771418"))

	def test_retry_refused_when_db_id_set(self):
		from dialogbrain.api.inbox import retry

		name = self._send()["name"]
		frappe.db.set_value("DB Message", name, {"delivery_status": "failed", "db_id": "123"})
		with self.assertRaises(frappe.ValidationError):
			retry(name)
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "failed")

	def test_record_failure_with_broken_rollback_leaves_claim(self):
		name = self._send()["name"]
		calls = []

		def rollback(save_point=None, **kw):
			calls.append(save_point)
			if save_point:
				raise frappe.QueryDeadlockError("deadlock")
			# a full rollback would wipe the test's own data: emulate it as a no-op here

		with patch("dialogbrain.sync.sender._record_sent", side_effect=frappe.QueryDeadlockError), patch.object(
			frappe.db, "rollback", side_effect=rollback
		):
			self._deliver(name, _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND)))
		self.assertEqual(calls, ["dialogbrain_record_sent", None])
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "unknown")

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

	def test_deliver_http_500_marks_unknown(self):
		"""F8: a 500 can come after DialogBrain sent the message: not retryable."""
		name = self._send()["name"]
		client = _mock_client(
			execute=lambda *a, **k: (_ for _ in ()).throw(DialogBrainError("HTTP 500", status=500))
		)
		self._deliver(name, client)
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "unknown")
		from dialogbrain.api.inbox import retry

		with self.assertRaises(frappe.ValidationError):
			retry(name)

	def test_deliver_enqueued_at_front_with_job_id(self):
		"""F7: a manager's reply jumps the webhook backlog on ``short``; F3: one job per
		row (the stale-pending sweep re-enqueues with the same id)."""
		name = self._send()["name"]
		self.enqueue.assert_called_once()
		kwargs = self.enqueue.call_args.kwargs
		self.assertEqual(self.enqueue.call_args.args[0], "dialogbrain.sync.sender.deliver")
		self.assertEqual(kwargs["queue"], "short")
		self.assertTrue(kwargs["at_front"])
		self.assertEqual(kwargs["job_id"], f"dialogbrain_deliver_{name}")
		self.assertTrue(kwargs["deduplicate"])
		self.assertEqual(kwargs["message_name"], name)

	def test_stale_pending_reply_is_requeued(self):
		"""F3: a reply whose deliver job never ran (queue flushed, worker died before the
		claim) is re-enqueued by the tick; fresh pending, claimed and inbound rows are not."""
		from frappe.utils import add_to_date, now_datetime

		from dialogbrain.sync.sender import requeue_stale_replies

		stale = self._send(client_ref="stale")["name"]
		fresh = self._send(client_ref="fresh")["name"]
		claimed = self._send(client_ref="claimed")["name"]
		frappe.db.set_value("DB Message", claimed, "delivery_status", "unknown")
		old = add_to_date(now_datetime(), minutes=-5)
		frappe.db.sql(
			"update `tabDB Message` set modified = %s where name in %s", (old, (stale, claimed))
		)
		inbound = frappe.get_doc(
			{"doctype": "DB Message", "conversation": self.conv, "direction": "in", "db_id": "in-1", "text": "x"}
		).insert(ignore_permissions=True)
		frappe.db.sql(
			"update `tabDB Message` set delivery_status = 'pending', modified = %s where name = %s",
			(old, inbound.name),
		)
		with patch("frappe.enqueue") as enqueue:
			requeue_stale_replies()
		self.assertEqual([c.kwargs["message_name"] for c in enqueue.call_args_list], [stale])
		self.assertEqual(enqueue.call_args.kwargs["job_id"], f"dialogbrain_deliver_{stale}")
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
		# the re-run is safe: deliver's atomic claim sends a row at most once
		client = _mock_client(execute=lambda *a, **k: SEND)
		self._deliver(stale, client)
		self._deliver(stale, client)
		self.assertEqual(client.execute.call_count, 1)
		self.assertEqual(frappe.db.get_value("DB Message", stale, "delivery_status"), "sent")
		self.assertEqual(frappe.db.get_value("DB Message", fresh, "delivery_status"), "pending")

	def test_tick_requeues_stale_replies(self):
		from dialogbrain.sync import reconcile

		with patch.object(reconcile, "_settings", return_value=frappe._dict(connected=1, network_mode="public")), patch(
			"dialogbrain.sync.reconcile.requeue_stale_replies"
		) as sweep, patch("frappe.enqueue"):
			reconcile.tick()
		sweep.assert_called_once()

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

	# --- request_id (Plan 3 B4) -------------------------------------------------

	def _caps(self, **caps):
		frappe.db.set_single_value("DialogBrain Settings", "agent_capabilities", json.dumps(caps))

	def _send_state(self, name):
		raw = frappe.db.get_value("DB Message", name, "raw")
		raw = json.loads(raw) if isinstance(raw, str) else (raw or {})
		return raw.get("send")

	def _age_attempt(self, name, first=None, last=None):
		"""Move the stored first / last send attempt into the past."""
		raw = frappe.db.get_value("DB Message", name, "raw")
		raw = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
		from frappe.utils import now_datetime

		now = now_datetime()
		if first is not None:
			raw["send"]["first_attempt_at"] = str(now - first)
		if last is not None:
			raw["send"]["attempt_at"] = str(now - last)
		frappe.db.set_value("DB Message", name, "raw", json.dumps(raw))

	def _timeout_client(self, upload_file=None):
		return _mock_client(
			execute=lambda *a, **k: (_ for _ in ()).throw(DialogBrainTimeout("read timeout")),
			upload_file=upload_file,
		)

	def _retry(self, name):
		from dialogbrain.api.inbox import retry

		frappe.set_user(self.agent)
		try:
			with patch("frappe.enqueue") as enqueue:
				retry(name)
		finally:
			frappe.set_user("Administrator")
		return enqueue

	def test_send_body_has_no_request_id_without_capability(self):
		self._caps(request_id=False)
		name = self._send()["name"]
		client = _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND))
		self._deliver(name, client)
		self.assertEqual(client.execute.call_args.kwargs, {})
		self.assertIsNone(self._send_state(name))

	def test_send_body_carries_client_ref_as_request_id_with_capability(self):
		self._caps(request_id=True)
		name = self._send(client_ref="cref-rid-1")["name"]
		client = _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND))
		self._deliver(name, client)
		client.execute.assert_called_once_with(
			"messages.send", {"thread_id": THREAD_ID, "text": LONG_TEXT}, request_id="cref-rid-1"
		)
		state = self._send_state(name)
		self.assertEqual(state["request_id"], "cref-rid-1")
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "sent")

	def test_odd_client_ref_is_hashed_into_a_valid_request_id(self):
		import re

		self._caps(request_id=True)
		name = self._send(client_ref="weird ref / ü")["name"]
		client = _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND))
		self._deliver(name, client)
		rid = client.execute.call_args.kwargs["request_id"]
		self.assertRegex(rid, re.compile(r"^[A-Za-z0-9_.:-]{1,128}$"))

	def test_unknown_retried_with_capability_resends_same_params_and_request_id(self):
		self._caps(request_id=True)
		file_doc = frappe.get_doc(
			{"doctype": "File", "file_name": "offer.txt", "content": b"offer", "is_private": 0}
		).insert(ignore_permissions=True)
		name = self._send(client_ref="cref-unknown", file_urls=[file_doc.file_url])["name"]
		self._deliver(name, self._timeout_client(upload_file=lambda *a, **k: 4242))
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "unknown")
		self._age_attempt(name, last=timedelta(minutes=5))

		enqueue = self._retry(name)
		enqueue.assert_called_once()
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "pending")

		client = _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND), upload_file=lambda *a, **k: 9999)
		self._deliver(name, client)
		client.upload_file.assert_not_called()  # the first upload is reused: same params, same cache key
		client.execute.assert_called_once_with(
			"messages.send",
			{"thread_id": THREAD_ID, "text": LONG_TEXT, "attachments": [4242]},
			request_id="cref-unknown",
		)
		msg = frappe.get_doc("DB Message", name)
		self.assertEqual(msg.delivery_status, "sent")
		self.assertEqual([a.file_id for a in msg.attachments], ["4242"])

	def test_unknown_retry_refused_without_capability(self):
		self._caps(request_id=True)
		name = self._send()["name"]
		self._deliver(name, self._timeout_client())
		self._age_attempt(name, last=timedelta(minutes=5))
		self._caps(request_id=False)
		with self.assertRaises(frappe.ValidationError):
			self._retry(name)
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "unknown")

	def test_unknown_retry_refused_when_first_attempt_had_no_request_id(self):
		self._caps(request_id=False)
		name = self._send()["name"]
		self._deliver(name, self._timeout_client())
		self._caps(request_id=True)  # DialogBrain upgraded since: the first send has no cache entry
		with self.assertRaises(frappe.ValidationError):
			self._retry(name)

	def test_unknown_retry_refused_after_23_hours(self):
		self._caps(request_id=True)
		name = self._send()["name"]
		self._deliver(name, self._timeout_client())
		self._age_attempt(name, first=timedelta(hours=23, minutes=1), last=timedelta(minutes=5))
		with self.assertRaises(frappe.ValidationError):
			self._retry(name)
		self._age_attempt(name, first=timedelta(hours=22, minutes=59))
		self._retry(name)
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "pending")

	def test_unknown_retry_refused_while_first_attempt_may_still_run(self):
		"""DialogBrain caches only a finished send: a retry next to a still-running first
		attempt would run concurrently, so it waits until that attempt must be over."""
		self._caps(request_id=True)
		name = self._send()["name"]
		self._deliver(name, self._timeout_client())
		with self.assertRaises(frappe.ValidationError) as cm:
			self._retry(name)
		self.assertIn("minute", str(cm.exception))

	def test_unknown_with_db_id_never_retried(self):
		self._caps(request_id=True)
		name = self._send()["name"]
		self._deliver(name, self._timeout_client())
		self._age_attempt(name, last=timedelta(minutes=5))
		frappe.db.set_value("DB Message", name, "db_id", "555")
		with self.assertRaises(frappe.ValidationError):
			self._retry(name)

	def test_deliver_does_not_resend_stored_attempt_past_the_window(self):
		"""The window is checked again when the job runs (a requeued job can run late)."""
		self._caps(request_id=True)
		name = self._send()["name"]
		self._deliver(name, self._timeout_client())
		self._age_attempt(name, last=timedelta(minutes=5))
		self._retry(name)
		self._age_attempt(name, first=timedelta(hours=24))
		client = _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND))
		self._deliver(name, client)
		client.execute.assert_not_called()
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "unknown")

	def _offer_file(self):
		return frappe.get_doc(
			{"doctype": "File", "file_name": "offer.txt", "content": b"offer", "is_private": 0}
		).insert(ignore_permissions=True).file_url

	def _failing_client(self, upload_file=None):
		return _mock_client(
			execute=lambda *a, **k: (_ for _ in ()).throw(DialogBrainError("thread not found", code="NOT_FOUND")),
			upload_file=upload_file,
		)

	def test_failed_retry_inside_window_resends_identical_params_and_request_id(self):
		"""Fix round 1: a failed row (e.g. the R11 sweep's "not confirmed") may have been
		sent after all; only the identical params hit DialogBrain's cache entry."""
		self._caps(request_id=True)
		name = self._send(client_ref="cref-f", file_urls=[self._offer_file()])["name"]
		self._deliver(name, self._failing_client(upload_file=lambda *a, **k: 4242))
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "failed")
		self._retry(name)
		self.assertEqual(self._send_state(name)["request_id"], "cref-f")
		client = _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND), upload_file=lambda *a, **k: 9999)
		self._deliver(name, client)
		client.upload_file.assert_not_called()
		client.execute.assert_called_once_with(
			"messages.send",
			{"thread_id": THREAD_ID, "text": LONG_TEXT, "attachments": [4242]},
			request_id="cref-f",
		)

	def test_failed_retry_outside_window_rebuilds_params_with_a_new_request_id(self):
		"""Past 23 h DialogBrain may have dropped the entry: the attempt is rebuilt (fresh
		upload) under a NEW request_id, so a surviving old entry can never stand in for a
		message whose params changed."""
		self._caps(request_id=True)
		name = self._send(client_ref="cref-old", file_urls=[self._offer_file()])["name"]
		self._deliver(name, self._failing_client(upload_file=lambda *a, **k: 4242))
		self._age_attempt(name, first=timedelta(hours=24))
		self._retry(name)
		self.assertIsNone(self._send_state(name))
		client = _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND), upload_file=lambda *a, **k: 9999)
		self._deliver(name, client)
		client.upload_file.assert_called_once()
		args, kwargs = client.execute.call_args
		self.assertEqual(args[1]["attachments"], [9999])
		self.assertNotEqual(kwargs["request_id"], "cref-old")
		self.assertTrue(kwargs["request_id"].startswith("cref-old"))

	def test_failed_retry_without_capability_is_plan_1(self):
		self._caps(request_id=True)
		name = self._send()["name"]
		self._deliver(name, self._failing_client())
		self._caps(request_id=False)
		self._retry(name)
		client = _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND))
		self._deliver(name, client)
		self.assertEqual(client.execute.call_args.kwargs, {})

	def test_ignored_request_id_clears_the_capability(self):
		"""DialogBrain did not echo the request_id (e.g. rolled back): it does not dedupe,
		so the capability is cleared and the row is never retried as unknown."""
		from dialogbrain.setup.agent_integration import has_capability

		self._caps(request_id=True, agent_pause=True)
		name = self._send()["name"]
		with patch("frappe.log_error"):
			self._deliver(name, _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND), echo=False))
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "sent")
		self.assertTrue(self._send_state(name)["request_id_ignored"])
		self.assertFalse(has_capability("request_id"))
		self.assertTrue(has_capability("agent_pause"))
		other = self._send(client_ref="cref-next")["name"]
		client = _mock_client(execute=lambda *a, **k: copy.deepcopy(SEND))
		self._deliver(other, client)
		self.assertEqual(client.execute.call_args.kwargs, {})

	def test_ignored_request_id_on_a_failure_is_not_reused(self):
		self._caps(request_id=True)
		name = self._send()["name"]
		with patch("frappe.log_error"):
			self._deliver(name, _mock_client(
				execute=lambda *a, **k: (_ for _ in ()).throw(DialogBrainError("bad", code="x")), echo=False
			))
		self._caps(request_id=True)  # even if it comes back
		self._retry(name)
		self.assertIsNone(self._send_state(name))

	def test_ambiguous_outcome_class_is_recorded(self):
		self._caps(request_id=True)
		cases = (
			(DialogBrainTimeout("read timeout"), "timeout"),
			(DialogBrainError("HTTP 504", status=504), "http_504"),
			(DialogBrainError("HTTP 502", status=502), "http_502"),
			(DialogBrainError("HTTP 500", status=500), "http_500"),
			(RuntimeError("boom"), "crash"),
		)
		for i, (exc, outcome) in enumerate(cases):
			name = self._send(client_ref=f"cref-o{i}")["name"]

			def raise_(*a, _exc=exc, **k):
				raise _exc

			with patch("frappe.log_error"):
				self._deliver(name, _mock_client(execute=raise_))
			self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "unknown")
			self.assertEqual(self._send_state(name)["outcome"], outcome)

	def test_messages_say_when_unknown_can_be_retried(self):
		from dialogbrain.api.conversations import get_messages

		self._caps(request_id=True)
		name = self._send()["name"]
		self._deliver(name, self._timeout_client())

		def flag():
			rows = {m["name"]: m for m in get_messages(self.conv)["rows"]}
			return rows[name]["can_retry"]

		self.assertTrue(flag())
		self._caps(request_id=False)
		self.assertFalse(flag())
