import hashlib
import hmac
import json
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from dialogbrain.api import webhook
from dialogbrain.sync.signature import verify_signature
from dialogbrain.tests.utils import DialogBrainTestCase, load_fixture

SECRET = "whsec_" + "ab" * 32


def sign(body: bytes, secret=SECRET) -> str:
	key = bytes.fromhex(secret.removeprefix("whsec_"))
	return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


class TestSignature(DialogBrainTestCase):
	def test_valid_signature(self):
		body = b'{"a":1}'
		self.assertTrue(verify_signature(body, sign(body), SECRET))

	def test_invalid_signature(self):
		body = b'{"a":1}'
		self.assertFalse(verify_signature(body + b"x", sign(body), SECRET))
		self.assertFalse(verify_signature(body, "sha256=00", SECRET))
		self.assertFalse(verify_signature(body, sign(body), "whsec_" + "cd" * 32))

	def test_missing_header(self):
		self.assertFalse(verify_signature(b"{}", None, SECRET))
		self.assertFalse(verify_signature(b"{}", "", SECRET))


class TestReceive(DialogBrainTestCase):
	def setUp(self):
		self.body = json.dumps(load_fixture("webhook_message_received_inbound")).encode()
		self.delivery_id = json.loads(self.body)["delivery_id"]
		frappe.db.delete("DB Webhook Log", {"delivery_id": self.delivery_id})
		settings = frappe.get_single("DialogBrain Settings")
		self._orig = (settings.connected, settings.get_password("webhook_secret", raise_exception=False))
		settings.connected = 1
		settings.webhook_secret = SECRET
		settings.flags.dialogbrain_server_write = True  # server-managed fields (R14)
		settings.save(ignore_permissions=True)
		frappe.local.response = frappe._dict()

	def tearDown(self):
		settings = frappe.get_single("DialogBrain Settings")
		settings.connected = self._orig[0]
		settings.webhook_secret = self._orig[1]
		settings.flags.dialogbrain_server_write = True  # server-managed fields (R14)
		settings.save(ignore_permissions=True)
		frappe.local.request = None

	def _post(self, body=None, signature="auto"):
		body = self.body if body is None else body
		headers = {}
		if signature == "auto":
			headers["X-DialogBrain-Signature"] = sign(body)
		elif signature:
			headers["X-DialogBrain-Signature"] = signature
		frappe.local.request = SimpleNamespace(
			get_data=lambda: body, content_length=len(body), headers=headers
		)
		frappe.local.response = frappe._dict()
		return webhook.receive()

	def _rows(self):
		return frappe.db.count("DB Webhook Log", {"delivery_id": self.delivery_id})

	def test_receive_stores_and_kicks_the_drain(self):
		"""One drain job per lane, not one RQ job per delivery: a burst of thousands of
		deliveries must never fill the RQ queue (frappe.QueueOverloaded -> 503 -> retries)."""
		frappe.db.after_commit.reset()
		with patch("frappe.enqueue") as enq:
			res = self._post()
			enq.assert_not_called()  # the kick waits for the row's commit
			frappe.db.after_commit.run()
		self.assertTrue(res["ok"])
		self.assertEqual(self._rows(), 1)
		row = frappe.get_doc("DB Webhook Log", {"delivery_id": self.delivery_id})
		self.assertEqual((row.status, row.event, row.lane), ("queued", "message.received", "short"))
		self.assertEqual(json.loads(row.payload)["delivery_id"], self.delivery_id)
		enq.assert_called_once()
		self.assertEqual(enq.call_args.args[0], "dialogbrain.sync.processor.drain")
		self.assertEqual(enq.call_args.kwargs["lane"], "short")
		self.assertEqual(enq.call_args.kwargs["queue"], "short")
		self.assertEqual(enq.call_args.kwargs["job_id"], "dialogbrain_drain_short")
		self.assertTrue(enq.call_args.kwargs["deduplicate"])
		self.assertFalse(enq.call_args.kwargs.get("enqueue_after_commit"))

	def test_receive_routes_skip_push_to_the_long_lane(self):
		"""F7: DialogBrain's backfill rows (``skip_push``) can be thousands of deliveries;
		they go to ``long`` so replies and live messages on ``short`` don't wait."""
		body = json.loads(self.body)
		body["data"]["skip_push"] = True
		raw = json.dumps(body).encode()
		frappe.db.after_commit.reset()
		with patch("frappe.enqueue") as enq:
			self._post(body=raw)
			frappe.db.after_commit.run()
		row = frappe.get_doc("DB Webhook Log", {"delivery_id": self.delivery_id})
		self.assertEqual(row.lane, "long")
		enq.assert_called_once()
		self.assertEqual(enq.call_args.kwargs["queue"], "long")
		self.assertEqual(enq.call_args.kwargs["lane"], "long")
		self.assertEqual(enq.call_args.kwargs["job_id"], "dialogbrain_drain_long")

	def test_receive_drops_backfill_rows_older_than_the_backfill_window(self):
		"""channel-sync re-emits every imported history row as message.received with
		``skip_push``; rows older than our own backfill horizon are not mirrored at all."""
		frappe.db.set_single_value("DialogBrain Settings", "backfill_days", 30, update_modified=False)
		body = json.loads(self.body)
		body["data"]["skip_push"] = True
		body["data"]["message"]["sent_at"] = "2025-01-01T10:00:00+00:00"
		frappe.db.after_commit.reset()
		with patch("frappe.enqueue") as enq:
			res = self._post(body=json.dumps(body).encode())
			frappe.db.after_commit.run()
		self.assertEqual(res, {"ok": True, "dropped": "history"})
		self.assertNotIn("http_status_code", frappe.local.response)
		self.assertEqual(self._rows(), 0)
		enq.assert_not_called()

	def test_receive_keeps_recent_skip_push_rows(self):
		body = json.loads(self.body)
		body["data"]["skip_push"] = True
		body["data"]["message"]["sent_at"] = (now_datetime() - timedelta(hours=1)).isoformat()
		with patch("frappe.enqueue"):
			res = self._post(body=json.dumps(body).encode())
		self.assertEqual(res, {"ok": True})
		self.assertEqual(self._rows(), 1)

	def test_receive_keeps_system_events_on_the_live_lane(self):
		"""A call marker carries ``skip_push`` too (nobody wrote to us) but it is live: it
		must not wait behind a backfill backlog on ``long``."""
		body = json.loads(self.body)
		body["data"]["skip_push"] = True
		body["data"]["message"]["is_system_event"] = True
		body["data"]["message"]["sent_at"] = (now_datetime() - timedelta(minutes=1)).isoformat()
		frappe.db.after_commit.reset()
		with patch("frappe.enqueue") as enq:
			self._post(body=json.dumps(body).encode())
			frappe.db.after_commit.run()
		row = frappe.get_doc("DB Webhook Log", {"delivery_id": self.delivery_id})
		self.assertEqual(row.lane, "short")
		self.assertEqual(enq.call_args.kwargs["lane"], "short")

	def test_receive_keeps_skip_push_rows_without_a_parsable_date(self):
		body = json.loads(self.body)
		body["data"]["skip_push"] = True
		body["data"]["message"]["sent_at"] = "yesterday-ish"
		with patch("frappe.enqueue"):
			res = self._post(body=json.dumps(body).encode())
		self.assertEqual(res, {"ok": True})
		self.assertEqual(self._rows(), 1)

	def test_kick_drain_survives_an_overloaded_queue(self):
		"""The row is stored and committed; frappe.QueueOverloaded must not turn into a 503
		(DialogBrain would retry 5 times). The drain catches the row later. One Error Log
		per lane per window, not one per delivery of a burst."""
		frappe.cache.delete_value("dialogbrain_drain_kick_error_short")
		with patch("frappe.enqueue", side_effect=frappe.QueueOverloaded("full")), patch(
			"frappe.log_error"
		) as log_error:
			webhook.kick_drain("short")  # does not raise
			webhook.kick_drain("short")
			webhook.kick_drain("short")
		log_error.assert_called_once()
		frappe.cache.delete_value("dialogbrain_drain_kick_error_short")

	def test_kick_drain_skips_enqueue_while_the_drain_is_queued_or_running(self):
		"""frappe.enqueue's own dedupe logs an error line per skipped kick; a burst is
		thousands of kicks, so the check is done here first."""
		with patch("dialogbrain.sync.processor.is_job_enqueued", return_value=True), patch(
			"frappe.enqueue"
		) as enq:
			webhook.kick_drain("long")
		enq.assert_not_called()

	def test_kick_drain_gives_the_job_a_long_timeout(self):
		"""RQ's queue default (300 s on short) would fire inside process_event and be
		swallowed as a failed delivery; the drain is bounded by max_batches instead."""
		with patch("dialogbrain.sync.processor.is_job_enqueued", return_value=False), patch(
			"frappe.enqueue"
		) as enq:
			webhook.kick_drain("short")
		self.assertGreaterEqual(enq.call_args.kwargs["timeout"], 3600)

	def test_receive_rejects_bad_signature(self):
		with patch("frappe.enqueue") as enq:
			self._post(signature="sha256=" + "0" * 64)
			self.assertEqual(frappe.local.response.http_status_code, 401)
			self._post(signature=None)
			self.assertEqual(frappe.local.response.http_status_code, 401)
		self.assertEqual(self._rows(), 0)
		enq.assert_not_called()

	def test_receive_rejects_large_body(self):
		big = b"x" * (1024 * 1024 + 1)
		with patch("frappe.enqueue") as enq:
			self._post(body=big)
		self.assertEqual(frappe.local.response.http_status_code, 413)
		enq.assert_not_called()

	def test_receive_dedupes_delivery_id(self):
		frappe.db.after_commit.reset()
		with patch("frappe.enqueue") as enq:
			r1 = self._post()
			r2 = self._post()
			frappe.db.after_commit.run()
		self.assertTrue(r1["ok"] and r2["ok"])
		self.assertEqual(r2.get("duplicate"), True)
		self.assertNotIn("http_status_code", frappe.local.response)
		self.assertEqual(self._rows(), 1)
		enq.assert_called_once()  # the duplicate stores nothing and kicks nothing

	def test_receive_when_not_connected_returns_200_and_drops(self):
		settings = frappe.get_single("DialogBrain Settings")
		settings.connected = 0
		settings.flags.dialogbrain_server_write = True  # server-managed fields (R14)
		settings.save(ignore_permissions=True)
		with patch("frappe.enqueue") as enq:
			res = self._post()
		self.assertTrue(res["ok"])
		self.assertNotIn("http_status_code", frappe.local.response)
		self.assertEqual(self._rows(), 0)
		enq.assert_not_called()
