"""Connect a channel from ERPNext, Task 2: the endpoints the dialog talks to."""

from unittest.mock import patch

import frappe

from dialogbrain import connect_gate
from dialogbrain.api import connect_channel as api
from dialogbrain.client import DialogBrainError, DialogBrainTimeout
from dialogbrain.tests import connect_fakes as fk
from dialogbrain.tests.utils import DialogBrainTestCase

CLAIM = fk.CLAIM
A1 = fk.PREFIX + "1"
FULL = {"whatsapp": ["qr", "pairing_code"], "telegram": ["qr", "phone"]}


class TestConnectChannel(DialogBrainTestCase):
	def setUp(self):
		fk.reset(FULL)
		self.agent, self.other, self.manager, self.plain = fk.people()
		self.fake = fk.FakeSessions()
		for target in ("dialogbrain.client.DialogBrainClient.from_settings", "dialogbrain.claims.frappe.enqueue"):
			p = patch(target, return_value=self.fake)
			p.start()
			self.addCleanup(p.stop)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user(self.agent)

	def row(self, name):
		return frappe.db.get_value(CLAIM, name, "*", as_dict=True)

	# --- who may start, and for whom ---

	def test_agent_owner_is_self(self):
		state = api.start("whatsapp", owner=self.other)
		row = self.row(state["claim"])
		self.assertEqual((row.requested_by, row.owner_user), (self.agent, self.agent))

	def test_manager_picks_owner_or_shared(self):
		frappe.set_user(self.manager)
		self.assertEqual(self.row(api.start("whatsapp", owner=self.agent)["claim"]).owner_user, self.agent)
		self.assertIsNone(self.row(api.start("whatsapp", owner="")["claim"]).owner_user)
		self.assertEqual(self.row(api.start("whatsapp")["claim"]).owner_user, self.manager)
		for bad in (self.plain, "nobody@example.com"):
			with self.assertRaises(frappe.ValidationError):
				api.start("whatsapp", owner=bad)

	def test_plain_user_refused(self):
		frappe.set_user(self.plain)
		for call in (api.options, lambda: api.start("whatsapp"), lambda: api.get("x")):
			with self.assertRaises(frappe.PermissionError):
				call()
		self.assertEqual(self.fake.calls, [])

	def test_feature_off_refused(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value(fk.SETTINGS, connect_gate.FIELD, None, update_modified=False)
		frappe.set_user(self.agent)
		self.assertIsNone(api.options()["channels"])
		with self.assertRaises(frappe.ValidationError):
			api.start("whatsapp")
		self.assertEqual(self.fake.calls, [])

	def test_unlisted_channel_or_method_refused(self):
		for channel, method in (("viber", "qr"), ("whatsapp", "phone"), ("telegram", "pairing_code"), ("", "qr")):
			with self.assertRaises(frappe.ValidationError):
				api.start(channel, method)
		self.assertEqual(frappe.db.count(CLAIM), 0)

	def test_options(self):
		self.assertEqual(api.options(), {"channels": FULL, "can_choose_owner": False})
		frappe.set_user(self.manager)
		self.assertTrue(api.options()["can_choose_owner"])

	# --- start / get ---

	def test_start_sends_external_ref(self):
		state = api.start("WhatsApp")
		self.assertEqual(
			self.fake.of(connect_gate.START),
			[{"channel": "whatsapp", "method": "qr", "external_ref": state["claim"]}],
		)
		self.assertEqual((state["status"], state["waiting"]), ("waiting_scan", True))
		self.assertTrue(state["qr_svg"].startswith("<svg"))
		self.assertNotIn("fake-qr-payload", state["qr_svg"])
		self.assertTrue(0 < state["expires_in"] <= api.DEFAULT_TTL)
		self.assertEqual(self.row(state["claim"]).session_id, "sess-1")

	def test_qr_image_passed_through_only_when_safe(self):
		self.fake.start_answer = {"status": "waiting_scan", "qr": "data:image/png;base64,AAAA"}
		state = api.start("whatsapp")
		self.assertEqual(state["qr_image"], "data:image/png;base64,AAAA")
		self.assertNotIn("qr_svg", state)
		self.fake.start_answer = {"status": "waiting_scan", "qr": 'data:text/html,<script>alert(1)</script>'}
		state = api.start("whatsapp")
		self.assertNotIn("qr_image", state)
		self.assertNotIn("qr_svg", state)

	def test_start_failure_is_explained_and_kept(self):
		for kind, text in (("billing_required", api.BILLING), ("permission_denied", api.NO_RIGHTS)):
			self.fake.fail[connect_gate.START] = DialogBrainError("no db_live_abcdef123 here", error_type=kind)
			with self.assertRaises(frappe.ValidationError) as ctx:
				api.start("whatsapp")
			self.assertIn(text, str(ctx.exception))
		self.fake.fail[connect_gate.START] = DialogBrainError("key db_live_abcdef123 " + "x" * 500)
		with self.assertRaises(frappe.ValidationError) as ctx:
			api.start("whatsapp")
		self.assertNotIn("db_live_abcdef123", str(ctx.exception))
		self.assertLessEqual(len(str(ctx.exception)), api.REASON_MAX)
		self.assertEqual(set(frappe.get_all(CLAIM, pluck="status")), {"failed"})

	def test_foreign_claim_not_found(self):
		name = api.start("whatsapp")["claim"]
		frappe.set_user(self.other)
		messages = set()
		for call in (
			lambda: api.get(name),
			lambda: api.submit(name, "code", "12345"),
			lambda: api.cancel(name),
			lambda: api.get("no-such-claim"),
			lambda: api.get(""),
		):
			with self.assertRaises(frappe.DoesNotExistError) as ctx:
				call()
			messages.add(str(ctx.exception))
		self.assertEqual(len(messages), 1)
		frappe.set_user(self.manager)  # a manager is not the requester either
		with self.assertRaises(frappe.DoesNotExistError):
			api.get(name)
		self.assertEqual(len(self.fake.calls), 1)  # only the start

	def test_get_refreshes_expiry(self):
		name = api.start("telegram")["claim"]
		self.fake.sessions["sess-1"] = {"status": "need_code", "expires_at": "2031-01-02T03:04:05Z"}
		state = api.get(name)
		self.assertEqual(state["status"], "need_code")
		self.assertEqual(self.row(name).expires_at.year, 2031)
		self.assertGreater(state["expires_in"], 1000)

	def test_get_connected_resolves(self):
		name = api.start("whatsapp")["claim"]
		self.fake.connect("sess-1", A1, phone="+10000000001")
		state = api.get(name)
		self.assertEqual((state["status"], state["channel"], state["message"]), ("connected", A1, None))
		self.assertEqual(fk.allowed(A1), [self.agent])
		calls = len(self.fake.calls)
		self.assertEqual(api.get(name)["channel"], A1)  # answered from the claim
		self.assertEqual(len(self.fake.calls), calls)

	def test_get_connected_number_of_someone_else(self):
		from dialogbrain import claims
		from dialogbrain.sync.upsert import upsert_channel

		upsert_channel(A1, "whatsapp", check_claims=False)
		claims._set_allowed(A1, [self.other])
		name = api.start("whatsapp")["claim"]
		self.fake.connect("sess-1", A1)
		state = api.get(name)
		self.assertEqual(state["message"], "This number is already assigned to another employee")
		self.assertNotIn("cc-other", str(state))

	def test_expired_and_error_close_the_claim(self):
		first = api.start("whatsapp")["claim"]
		self.fake.sessions["sess-1"] = {"status": "expired"}
		self.assertEqual(api.get(first)["status"], "expired")
		second = api.start("whatsapp")["claim"]
		self.fake.sessions["sess-2"] = {"status": "error", "error": "device limit reached db_live_zzz999"}
		state = api.get(second)
		self.assertEqual((state["status"], state["error"]), ("error", "device limit reached [hidden]"))
		self.assertEqual((self.row(first).status, self.row(second).status), ("expired", "failed"))
		self.assertEqual(api.get(second)["status"], "error")

	def test_cancelled_by_dialogbrain_reads_as_expired(self):
		name = api.start("whatsapp")["claim"]
		self.fake.sessions["sess-1"] = {"status": "cancelled"}
		state = api.get(name)
		self.assertEqual((state["status"], state["waiting"]), ("expired", False))
		self.assertEqual(self.row(name).status, "expired")

	def test_pairing_code_shown(self):
		name = api.start("whatsapp", "pairing_code")["claim"]
		self.fake.sessions["sess-1"] = {"status": "need_pairing_code", "pairing_code": "ABCD-1234"}
		self.assertEqual(api.submit(name, "phone", " +10000000001 ")["pairing_code"], "ABCD-1234")
		self.assertEqual(self.fake.of(connect_gate.SUBMIT), [{"session_id": "sess-1", "phone": "+10000000001"}])

	# --- submit ---

	def test_submit_passes_kind(self):
		name = api.start("telegram", "phone")["claim"]
		self.fake.sessions["sess-1"] = {"status": "need_password"}
		self.assertEqual(api.submit(name, "code", "12345")["status"], "need_password")
		self.assertEqual(self.fake.of(connect_gate.SUBMIT), [{"session_id": "sess-1", "code": "12345"}])
		for kind, secret in (("otp", "1"), ("session_id", "x"), ("code", ""), ("code", "9" * 201), ("code", None), ("code", 771234)):
			with self.assertRaises(frappe.ValidationError):
				api.submit(name, kind, secret)
		self.assertEqual(len(self.fake.of(connect_gate.SUBMIT)), 1)

	def test_submit_secret_not_logged(self):
		name = api.start("telegram", "phone")["claim"]
		before = frappe.db.count("Error Log")
		frappe.local.form_dict.update({"secret": "771234", "kind": "code"})
		for exc in (RuntimeError("boom 771234"), DialogBrainError("bad code 771234")):
			self.fake.fail[connect_gate.SUBMIT] = exc
			with self.assertRaises(frappe.ValidationError) as ctx:
				api.submit(name, "code", "771234")
			self.assertNotIn("771234", str(ctx.exception))
			self.assertIsNone(ctx.exception.__cause__)
		self.assertNotIn("771234", str(dict(frappe.local.form_dict)))
		self.assertEqual(frappe.db.count("Error Log"), before)
		self.assertNotIn("771234", str(self.row(name)))

	def test_submit_rate_limit(self):
		name = api.start("telegram", "phone")["claim"]
		self.fake.sessions["sess-1"] = {"status": "need_code"}
		for _ in range(api.SUBMIT_LIMIT[0]):
			api.submit(name, "code", "00000")
		with self.assertRaises(frappe.TooManyRequestsError):
			api.submit(name, "code", "00000")

	def test_start_rate_limit(self):
		for _ in range(api.START_LIMIT[0]):
			api.start("whatsapp")
		with self.assertRaises(frappe.TooManyRequestsError):
			api.start("whatsapp")
		frappe.set_user(self.other)  # per user
		api.start("whatsapp")

	# --- cancel ---

	def test_cancel_only_while_waiting(self):
		waiting = api.start("whatsapp")["claim"]
		self.assertEqual(api.cancel(waiting), {"ok": True})
		self.assertEqual(self.fake.of(connect_gate.CANCEL), [{"session_id": "sess-1"}])
		self.assertEqual(self.row(waiting).status, "cancelled")
		api.cancel(waiting)  # again: nothing more
		self.assertEqual(len(self.fake.of(connect_gate.CANCEL)), 1)

	def test_cancel_racing_a_connection_keeps_the_number(self):
		name = api.start("whatsapp")["claim"]
		real = self.fake.execute

		def execute(tool_id, params, **kwargs):
			if tool_id == connect_gate.CANCEL:  # connected a moment before: cancel is a no-op
				self.fake.calls.append((tool_id, dict(params)))
				self.fake.connect("sess-1", A1)
				return {"ok": True}
			return real(tool_id, params, **kwargs)

		with patch.object(self.fake, "execute", side_effect=execute):
			api.cancel(name)
		self.assertEqual(self.row(name).status, "connected")
		self.assertEqual(fk.allowed(A1), [self.agent])

	def test_cancel_when_dialogbrain_is_unreachable_decides_nothing(self):
		from dialogbrain import claims

		name = api.start("whatsapp")["claim"]
		self.fake.fail[connect_gate.GET] = DialogBrainTimeout("slow")
		self.assertEqual(api.cancel(name), {"ok": True})
		self.assertEqual(self.row(name).status, "open")  # it may have connected just now
		del self.fake.fail[connect_gate.GET]
		self.fake.connect("sess-1", A1)
		claims.sweep()
		self.assertEqual(fk.allowed(A1), [self.agent])

	def test_start_timeout_keeps_the_claim_open(self):
		from dialogbrain.sync.upsert import upsert_channel

		self.fake.fail[connect_gate.START] = DialogBrainTimeout("slow")
		with self.assertRaises(frappe.ValidationError) as ctx:
			api.start("whatsapp")
		self.assertIn(api.UNSURE, str(ctx.exception))
		row = frappe.get_all(CLAIM, fields=["name", "status", "session_id", "owner_user"])[0]
		self.assertEqual((row.status, row.session_id, row.owner_user), ("open", None, self.agent))
		# the session did start in DialogBrain and the number connects: never open to all
		upsert_channel(A1, "whatsapp")
		self.assertEqual(frappe.db.get_value("DB Channel", A1, "restricted"), 1)
		upsert_channel(fk.PREFIX + "2", "whatsapp", external_ref=row.name)
		self.assertEqual(fk.allowed(fk.PREFIX + "2"), [self.agent])
		# the dialog of that claim: start again, nothing to send or cancel
		self.assertEqual(api.cancel(row.name), {"ok": True})

	def test_unanswered_start_shows_start_again(self):
		name = fk.claim(self.agent, session_id=None)
		self.assertEqual(api.get(name)["status"], "expired")
		with self.assertRaises(frappe.DoesNotExistError):
			api.submit(name, "code", "12345")
		self.assertEqual(self.fake.calls, [])

	def test_cancel_after_connected_keeps_the_number(self):
		seen = api.start("whatsapp")["claim"]
		self.fake.connect("sess-1", A1)
		api.get(seen)
		api.cancel(seen)
		# connected between the last poll and closing the dialog
		late = api.start("whatsapp")["claim"]
		self.fake.connect("sess-2", fk.PREFIX + "2")
		api.cancel(late)
		self.assertEqual(len(self.fake.of(connect_gate.CANCEL)), 0)
		self.assertEqual((self.row(seen).status, self.row(late).status), ("connected", "connected"))
		self.assertEqual(fk.allowed(fk.PREFIX + "2"), [self.agent])
