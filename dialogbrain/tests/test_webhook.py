import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import patch

import frappe

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
		settings.save(ignore_permissions=True)
		frappe.local.response = frappe._dict()

	def tearDown(self):
		settings = frappe.get_single("DialogBrain Settings")
		settings.connected = self._orig[0]
		settings.webhook_secret = self._orig[1]
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

	def test_receive_stores_and_enqueues(self):
		with patch("frappe.enqueue") as enq:
			res = self._post()
		self.assertTrue(res["ok"])
		self.assertEqual(self._rows(), 1)
		row = frappe.get_doc("DB Webhook Log", {"delivery_id": self.delivery_id})
		self.assertEqual(row.status, "queued")
		self.assertEqual(row.event, "message.received")
		self.assertEqual(json.loads(row.payload)["delivery_id"], self.delivery_id)
		enq.assert_called_once()
		self.assertEqual(enq.call_args.args[0], "dialogbrain.sync.processor.process_delivery")
		self.assertEqual(enq.call_args.kwargs["log_name"], row.name)
		self.assertEqual(enq.call_args.kwargs["queue"], "short")
		self.assertTrue(enq.call_args.kwargs["enqueue_after_commit"])
		# same job id as the scheduler's retry, so one delivery is never queued twice
		self.assertEqual(enq.call_args.kwargs["job_id"], f"dialogbrain_delivery_{row.name}")
		self.assertTrue(enq.call_args.kwargs["deduplicate"])

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
		with patch("frappe.enqueue") as enq:
			r1 = self._post()
			r2 = self._post()
		self.assertTrue(r1["ok"] and r2["ok"])
		self.assertNotIn("http_status_code", frappe.local.response)
		self.assertEqual(self._rows(), 1)
		enq.assert_called_once()

	def test_receive_when_not_connected_returns_200_and_drops(self):
		settings = frappe.get_single("DialogBrain Settings")
		settings.connected = 0
		settings.save(ignore_permissions=True)
		with patch("frappe.enqueue") as enq:
			res = self._post()
		self.assertTrue(res["ok"])
		self.assertNotIn("http_status_code", frappe.local.response)
		self.assertEqual(self._rows(), 0)
		enq.assert_not_called()
