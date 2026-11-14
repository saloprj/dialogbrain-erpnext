"""Tests for dialogbrain.thread_context (Plan 3, B2): signed thread-context headers.

Verification runs against real werkzeug requests built with ``EnvironBuilder`` and set as
``frappe.local.request``, so the path, method and body that are checked are exactly what
the WSGI layer received.
"""

import hashlib
import hmac
import json

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from dialogbrain import thread_context as tc
from dialogbrain.tests.test_inbox_api import _channel, _conv
from dialogbrain.tests.test_party_card import _customer
from dialogbrain.tests.utils import DialogBrainTestCase

SETTINGS = "DialogBrain Settings"
SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
TS = 1794571200
THREAD = "4217"
ORDER_PATH = "/api/method/dialogbrain.agent_api.order_status"
FIND_PATH = "/api/method/dialogbrain.agent_api.find_customer"
ORDER_BODY = b'{"order_no":"SAL-ORD-2026-00001"}'
TEST_INSTANCE = "aaaaaaaaaaaaaaaa"

# Worked examples DialogBrain's signer is pinned to (secret SECRET, timestamp TS, thread 4217).
VECTOR_POST = "sha256=59a0edf857115d45b3fc7831edb6fa926ac74c59d42fc3b4055c2b3958e120b6"
VECTOR_GET = "sha256=0dedd400c937104a0e502fa571f7c006487ec2e53c2116eabf493b45d578de1d"


def set_secret(secret=SECRET):
	from frappe.utils.password import set_encrypted_password

	set_encrypted_password(SETTINGS, SETTINGS, secret, "thread_context_secret")


def expected_signature(secret, ts, thread, method, path, body):
	"""The contract, written out independently of the code under test."""
	msg = f"{ts}.{thread}.{method}.{path}.{hashlib.sha256(body).hexdigest()}"
	return "sha256=" + hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def make_request(path=ORDER_PATH, body=ORDER_BODY, method="POST", headers=None, raw_uri=None, query=None,
				 content_type="application/json"):
	environ = {}
	if raw_uri is not None:
		environ["RAW_URI"] = raw_uri
	builder = EnvironBuilder(path=path, method=method, data=body, headers=headers or {},
							 query_string=query, content_type=content_type if body else None,
							 environ_overrides=environ)
	try:
		return Request(builder.get_environ())
	finally:
		builder.close()


def signed_headers(thread=THREAD, ts=TS, method="POST", path=ORDER_PATH, body=ORDER_BODY, secret=SECRET):
	return {
		"X-DialogBrain-Thread-Id": str(thread),
		"X-DialogBrain-Timestamp": str(ts),
		"X-DialogBrain-Context-Signature": expected_signature(secret, ts, thread, method, path, body),
	}


class TestThreadContext(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for dt in ("DB Message", "DB Conversation", "DB Channel User", "DB Channel"):
			frappe.db.delete(dt)
		set_secret()
		frappe.db.set_single_value(SETTINGS, "dialogbrain_instance", TEST_INSTANCE)
		self.channel = _channel("ctx-ch", "telegram")
		self.customer = _customer("Ctx Customer")
		self.conv = _conv(self.channel, thread_id=THREAD, party_type="Customer", party=self.customer,
						  link_source="manual")

	def verify(self, request, now=TS):
		return tc.verify(request, now=now)

	# --- signature vectors ------------------------------------------------------------

	def test_worked_examples(self):
		self.assertEqual(tc.signature(SECRET, TS, THREAD, "POST", ORDER_PATH, ORDER_BODY), VECTOR_POST)
		self.assertEqual(tc.signature(SECRET, TS, THREAD, "GET", FIND_PATH, b""), VECTOR_GET)

	def test_spaced_json_body_vector(self):
		"""DialogBrain's httpx bodies use json.dumps defaults (spaces after ':' and ',')."""
		body = json.dumps({"order_no": "SAL-ORD-2026-00001", "x": 1}).encode()
		self.assertIn(b": ", body)
		self.assertEqual(tc.signature(SECRET, TS, THREAD, "POST", ORDER_PATH, body),
						 expected_signature(SECRET, TS, THREAD, "POST", ORDER_PATH, body))
		ctx = self.verify(make_request(body=body, headers=signed_headers(body=body)))
		self.assertIsNotNone(ctx)
		self.assertEqual(ctx.conversation, self.conv)

	# --- valid context ----------------------------------------------------------------

	def test_valid_post_vector_returns_the_identified_context(self):
		headers = signed_headers()
		self.assertEqual(headers["X-DialogBrain-Context-Signature"], VECTOR_POST)
		ctx = self.verify(make_request(headers=headers))
		self.assertEqual(ctx, tc.Context(thread_id=THREAD, conversation=self.conv, party_type="Customer",
										 party=self.customer, identified=True))

	def test_valid_get_vector_with_empty_body(self):
		headers = signed_headers(method="GET", path=FIND_PATH, body=b"")
		self.assertEqual(headers["X-DialogBrain-Context-Signature"], VECTOR_GET)
		ctx = self.verify(make_request(path=FIND_PATH, body=b"", method="GET", headers=headers))
		self.assertTrue(ctx.identified)

	def test_query_string_is_refused(self):
		"""Ruling R9: Frappe merges query args into the tool arguments and the signature
		does not cover them, so a signed empty-body call replayed with ``?order_no=``
		must not verify."""
		headers = signed_headers(body=b"")
		self.assertTrue(self.verify(make_request(body=b"", headers=headers)).identified)
		req = make_request(body=b"", query="order_no=SAL-ORD-2026-00099", headers=headers)
		self.assertEqual(tc.resolve(req, now=TS), (None, tc.QUERY_STRING))
		raw = ORDER_PATH + "?order_no=SAL-ORD-2026-00099"
		req = make_request(body=b"", raw_uri=raw, headers=headers)
		self.assertEqual(tc.resolve(req, now=TS), (None, tc.QUERY_STRING))
		# an empty query ("...?") carries no arguments
		self.assertTrue(self.verify(make_request(body=b"", raw_uri=ORDER_PATH + "?", headers=headers)).identified)

	def test_conversation_of_another_instance_is_not_identified(self):
		"""Final wave F3b: the thread id of a chat stamped by another DialogBrain instance
		(or workspace) may name a different chat here: treated as unknown."""
		frappe.db.set_value("DB Conversation", self.conv, "dialogbrain_instance", "bbbbbbbbbbbbbbbb")
		self.assertEqual(tc.resolve(make_request(headers=signed_headers()), now=TS), (None, tc.OTHER_INSTANCE))
		frappe.db.set_value("DB Conversation", self.conv, "dialogbrain_instance", TEST_INSTANCE)
		self.assertTrue(self.verify(make_request(headers=signed_headers())).identified)

	def _resolve_with(self, settings_key, conv_key):
		frappe.db.set_single_value(SETTINGS, "dialogbrain_instance", settings_key)
		frappe.db.set_value("DB Conversation", self.conv, "dialogbrain_instance", conv_key)
		return tc.resolve(make_request(headers=signed_headers()), now=TS)

	def test_empty_instance_keys_never_match(self):
		"""Residual fix R1: an empty key on either side is not identified, even when both
		are empty."""
		for settings_key, conv_key in ((None, None), (None, TEST_INSTANCE), (TEST_INSTANCE, None)):
			self.assertEqual(self._resolve_with(settings_key, conv_key), (None, tc.OTHER_INSTANCE),
							 (settings_key, conv_key))

	def test_auto_verified_link_is_identified(self):
		frappe.db.set_value("DB Conversation", self.conv, "link_source", "auto_verified")
		self.assertTrue(self.verify(make_request(headers=signed_headers())).identified)

	def test_timestamp_within_300_seconds(self):
		for skew in (-300, 300):
			self.assertIsNotNone(self.verify(make_request(headers=signed_headers()), now=TS + skew), skew)

	def test_reads_the_secret_from_the_password_field(self):
		set_secret("f" * 64)
		self.assertIsNone(self.verify(make_request(headers=signed_headers())))
		ctx = self.verify(make_request(headers=signed_headers(secret="f" * 64)))
		self.assertTrue(ctx.identified)

	def test_uses_the_raw_request_uri(self):
		"""The signature covers the path as sent (RAW_URI), not a re-built one."""
		ctx = self.verify(make_request(raw_uri=ORDER_PATH, headers=signed_headers()))
		self.assertTrue(ctx and ctx.identified)
		# percent-encoded path: werkzeug decodes request.path, RAW_URI keeps the bytes as sent
		encoded = "/api/method/dialogbrain.agent_api.order%5Fstatus"
		req = make_request(raw_uri=encoded, headers=signed_headers(path=encoded))
		self.assertTrue(self.verify(req).identified)
		req = make_request(raw_uri=encoded, headers=signed_headers(path=ORDER_PATH))
		self.assertIsNone(self.verify(req))

	def test_request_uri_fallback(self):
		builder = EnvironBuilder(path=ORDER_PATH, method="POST", data=ORDER_BODY, headers=signed_headers(),
								 content_type="application/json",
								 environ_overrides={"REQUEST_URI": ORDER_PATH})
		req = Request(builder.get_environ())
		self.assertTrue(self.verify(req).identified)

	def test_hashes_the_raw_body_bytes(self):
		"""The body is hashed as received, never re-serialized: the same JSON with other
		spacing is a different body."""
		spaced = b'{"order_no": "SAL-ORD-2026-00001"}'
		req = make_request(body=spaced, headers=signed_headers(body=ORDER_BODY))
		self.assertIsNone(self.verify(req))

	def test_form_body_read_like_frappe_still_verifies(self):
		"""make_form_dict calls get_data() before .form, so a form body hashes its real bytes."""
		body = b"order_no=SAL-ORD-2026-00001"
		req = make_request(body=body, headers=signed_headers(body=body),
						   content_type="application/x-www-form-urlencoded")
		req.get_data(as_text=True)
		self.assertEqual(req.form.get("order_no"), "SAL-ORD-2026-00001")
		self.assertTrue(self.verify(req).identified)

	def test_body_already_read_by_frappe_still_verifies(self):
		req = make_request(headers=signed_headers())
		self.assertEqual(json.loads(req.get_data(as_text=True)), {"order_no": "SAL-ORD-2026-00001"})
		self.assertTrue(self.verify(req).identified)

	def test_defaults_to_the_current_request(self):
		frappe.local.request = make_request(headers=signed_headers())
		try:
			from unittest.mock import patch

			with patch.object(tc.time, "time", return_value=TS + 10):
				ctx = tc.verify()
		finally:
			del frappe.local.request
		self.assertTrue(ctx.identified)

	# --- not identified (Review Focus 1, 2) -------------------------------------------

	def test_no_request_or_no_headers(self):
		self.assertIsNone(self.verify(None))
		self.assertIsNone(self.verify(make_request()))

	def test_each_missing_header(self):
		for header in signed_headers():
			headers = signed_headers()
			del headers[header]
			self.assertIsNone(self.verify(make_request(headers=headers)), header)

	def test_bad_signature(self):
		headers = signed_headers()
		headers["X-DialogBrain-Context-Signature"] = "sha256=" + "0" * 64
		self.assertIsNone(self.verify(make_request(headers=headers)))
		headers["X-DialogBrain-Context-Signature"] = VECTOR_POST[len("sha256="):]  # no prefix
		self.assertIsNone(self.verify(make_request(headers=headers)))
		headers["X-DialogBrain-Context-Signature"] = "sha256=ünïcode"
		self.assertIsNone(self.verify(make_request(headers=headers)))

	def test_signature_from_another_secret(self):
		"""Another workspace (another secret) cannot sign for this site."""
		self.assertIsNone(self.verify(make_request(headers=signed_headers(secret="e" * 64))))

	def test_stale_or_future_timestamp(self):
		for skew in (-301, 301, 3600):
			self.assertIsNone(self.verify(make_request(headers=signed_headers()), now=TS + skew), skew)

	def test_malformed_timestamp_or_thread(self):
		for ts in ("abc", "1794571200.0", "", "-1"):
			headers = signed_headers()
			headers["X-DialogBrain-Timestamp"] = ts
			self.assertIsNone(self.verify(make_request(headers=headers)), ts)
		for thread in ("inbox-t1", "", "42 17", "４２１７"):
			self.assertIsNone(self.verify(make_request(headers=signed_headers(thread=thread))), thread)

	def test_another_chats_thread_id_is_refused(self):
		"""The thread id is signed: swapping it for another chat's id breaks the signature."""
		other = _conv(self.channel, thread_id="5555", party_type="Customer", party=_customer("Ctx Other"),
					  link_source="manual")
		headers = signed_headers()
		headers["X-DialogBrain-Thread-Id"] = "5555"
		self.assertIsNone(self.verify(make_request(headers=headers)))
		self.assertEqual(self.verify(make_request(headers=signed_headers(thread="5555"))).conversation, other)

	def test_signature_is_bound_to_path_method_and_body(self):
		# replayed against another tool
		self.assertIsNone(self.verify(make_request(path=FIND_PATH, headers=signed_headers())))
		# another method
		self.assertIsNone(self.verify(make_request(method="PUT", headers=signed_headers())))
		# another body (e.g. another order_no)
		body = b'{"order_no":"SAL-ORD-2026-00002"}'
		self.assertIsNone(self.verify(make_request(body=body, headers=signed_headers())))

	def test_only_agent_api_paths(self):
		path = "/api/method/frappe.client.get_list"
		self.assertIsNone(self.verify(make_request(path=path, headers=signed_headers(path=path))))
		path = "/api/method/dialogbrain.api.inbox.list_conversations"
		self.assertIsNone(self.verify(make_request(path=path, headers=signed_headers(path=path))))

	def test_unknown_thread(self):
		self.assertIsNone(self.verify(make_request(headers=signed_headers(thread="999999"))))

	def test_no_secret_configured(self):
		from frappe.utils.password import remove_encrypted_password

		remove_encrypted_password(SETTINGS, SETTINGS, "thread_context_secret")
		self.assertIsNone(self.verify(make_request(headers=signed_headers())))

	def test_auto_unverified_link_is_not_identified(self):
		frappe.db.set_value("DB Conversation", self.conv, "link_source", "auto_unverified")
		ctx = self.verify(make_request(headers=signed_headers()))
		self.assertEqual((ctx.conversation, ctx.identified), (self.conv, False))

	def test_unlinked_conversation_is_not_identified(self):
		frappe.db.set_value("DB Conversation", self.conv,
							{"party_type": None, "party": None, "link_source": None})
		ctx = self.verify(make_request(headers=signed_headers()))
		self.assertEqual((ctx.conversation, ctx.party, ctx.identified), (self.conv, None, False))

	def test_manual_link_without_party_is_not_identified(self):
		frappe.db.set_value("DB Conversation", self.conv, {"party_type": None, "party": None})
		self.assertFalse(self.verify(make_request(headers=signed_headers())).identified)

	# --- reason codes (Ruling R8) ------------------------------------------------------

	def reason(self, request, now=TS):
		return tc.resolve(request, now=now)[1]

	def test_reason_codes(self):
		self.assertEqual(tc.resolve(make_request(headers=signed_headers()), now=TS)[1], None)
		self.assertEqual(tc.resolve(None, now=TS), (None, tc.NO_HEADERS))
		self.assertEqual(self.reason(make_request()), tc.NO_HEADERS)
		headers = signed_headers()
		del headers["X-DialogBrain-Timestamp"]
		self.assertEqual(self.reason(make_request(headers=headers)), tc.NO_HEADERS)
		self.assertEqual(self.reason(make_request(query="x=1", headers=signed_headers())), tc.QUERY_STRING)
		bad = signed_headers()
		bad["X-DialogBrain-Context-Signature"] = "sha256=" + "0" * 64
		self.assertEqual(self.reason(make_request(headers=bad)), tc.BAD_SIGNATURE)
		self.assertEqual(self.reason(make_request(headers=signed_headers(thread="x1"))), tc.BAD_SIGNATURE)
		self.assertEqual(self.reason(make_request(headers=signed_headers()), now=TS + 301), tc.STALE)
		self.assertEqual(self.reason(make_request(headers=signed_headers(thread="999999"))), tc.UNKNOWN_THREAD)

	def test_reason_unlinked_and_unverified(self):
		frappe.db.set_value("DB Conversation", self.conv, "link_source", "auto_unverified")
		ctx, reason = tc.resolve(make_request(headers=signed_headers()), now=TS)
		self.assertEqual((ctx.conversation, ctx.identified, reason), (self.conv, False, tc.UNVERIFIED))
		frappe.db.set_value("DB Conversation", self.conv, {"party_type": None, "party": None})
		self.assertEqual(self.reason(make_request(headers=signed_headers())), tc.UNLINKED)
		# a manual link to a party that no longer exists is not identified either
		frappe.db.set_value("DB Conversation", self.conv,
							{"party_type": "Customer", "party": "Ctx Gone", "link_source": "manual"})
		ctx, reason = tc.resolve(make_request(headers=signed_headers()), now=TS)
		self.assertEqual((ctx.identified, reason), (False, tc.UNLINKED))

	def test_reason_no_secret(self):
		from frappe.utils.password import remove_encrypted_password

		remove_encrypted_password(SETTINGS, SETTINGS, "thread_context_secret")
		self.assertEqual(self.reason(make_request(headers=signed_headers())), tc.NO_SECRET)
