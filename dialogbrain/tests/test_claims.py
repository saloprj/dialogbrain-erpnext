"""Connect a channel from ERPNext, Task 3: the claim gives the channel its owner."""

from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from dialogbrain import claims
from dialogbrain.client import DialogBrainError
from dialogbrain.sync.upsert import upsert_channel
from dialogbrain.tests import connect_fakes as fk
from dialogbrain.tests.utils import DialogBrainTestCase

CLAIM = fk.CLAIM
A1, A2 = fk.PREFIX + "1", fk.PREFIX + "2"


class TestClaims(DialogBrainTestCase):
	def setUp(self):
		fk.reset()
		self.agent, self.other, self.manager, self.plain = fk.people()
		self.fake = fk.FakeSessions()
		for target in ("dialogbrain.client.DialogBrainClient.from_settings", "dialogbrain.claims.frappe.enqueue"):
			p = patch(target, return_value=self.fake)
			p.start()
			self.addCleanup(p.stop)
		self.addCleanup(frappe.set_user, "Administrator")

	def status(self, name):
		return frappe.db.get_value(CLAIM, name, "status")

	# --- resolve ---

	def test_resolve_new_channel_assigns_owner(self):
		name = fk.claim(self.agent)
		result = claims.resolve(name, {"account_id": A1, "name": "Sales phone"})
		self.assertEqual(result, {"channel": A1, "assigned": True, "message": None})
		self.assertEqual(fk.allowed(A1), [self.agent])
		row = frappe.db.get_value(CLAIM, name, ["status", "account_id", "resolved_at"], as_dict=True)
		self.assertEqual((row.status, row.account_id), ("connected", A1))
		self.assertTrue(row.resolved_at)
		self.assertEqual(frappe.db.get_value("DB Channel", A1, "title"), "Sales phone")

	def test_resolve_shared_leaves_allowed_empty(self):
		name = fk.claim(None, requested_by=self.manager)
		self.assertTrue(claims.resolve(name, {"account_id": A1})["assigned"])
		self.assertEqual(fk.allowed(A1), [])

	def test_resolve_existing_unowned_assigns(self):
		upsert_channel(A1, "whatsapp", check_claims=False)
		name = fk.claim(self.agent)
		self.assertTrue(claims.resolve(name, {"account_id": A1})["assigned"])
		self.assertEqual(fk.allowed(A1), [self.agent])

	def test_resolve_existing_other_owner_untouched(self):
		upsert_channel(A1, "whatsapp", check_claims=False)
		claims._set_allowed(A1, [self.other])
		name = fk.claim(self.agent)
		result = claims.resolve(name, {"account_id": A1})
		self.assertFalse(result["assigned"])
		self.assertEqual(result["message"], "This number is already assigned to another employee")
		self.assertNotIn("cc-other", str(result))
		self.assertEqual(fk.allowed(A1), [self.other])
		self.assertEqual(self.status(name), "connected")

	def test_resolve_twice_is_idempotent(self):
		name = fk.claim(self.agent)
		first = claims.resolve(name, {"account_id": A1})
		self.assertEqual(claims.resolve(name, {"account_id": A1}), first)
		self.assertEqual(claims.resolve(name, {}), first)
		self.assertEqual(fk.allowed(A1), [self.agent])
		self.assertEqual(frappe.db.count("DB Channel", {"name": A1}), 1)

	def test_resolve_as_chat_agent(self):
		name = fk.claim(self.agent)
		frappe.set_user(self.agent)
		claims.resolve(name, {"account_id": A1})
		frappe.set_user("Administrator")
		self.assertEqual(fk.allowed(A1), [self.agent])

	def test_resolve_without_account_does_nothing(self):
		name = fk.claim(self.agent)
		self.assertIsNone(claims.resolve(name, {"status": "connected"})["channel"])
		self.assertEqual(self.status(name), "open")

	def test_realtime_published_on_resolve(self):
		name = fk.claim(self.agent)
		with patch("dialogbrain.claims.frappe.publish_realtime") as publish:
			claims.resolve(name, {"account_id": A1})
			claims.resolve(name, {"account_id": A1})
		ours = [c for c in publish.call_args_list if c.args[:1] == ("dialogbrain:channel_connected",)]
		users = [c.kwargs["user"] for c in ours]
		self.assertEqual(len(users), len(set(users)))  # once per person
		self.assertIn(self.agent, users)
		self.assertIn(self.manager, users)
		self.assertNotIn(self.other, users)
		self.assertTrue(all(c.args[1] == {"channel": A1} and c.kwargs["after_commit"] for c in ours))

	# --- sweep: nobody is polling ---

	def test_sweep_resolves_closed_tab(self):
		name = fk.claim(self.agent)
		self.fake.connect("sess-1", A1)
		self.assertEqual(claims.sweep(), 1)
		self.assertEqual(fk.allowed(A1), [self.agent])
		self.assertEqual(self.status(name), "connected")

	def test_sweep_leaves_waiting_claims(self):
		name = fk.claim(self.agent)
		self.fake.sessions["sess-1"] = {"status": "waiting_scan"}
		self.assertEqual(claims.sweep(), 0)
		self.assertEqual(self.status(name), "open")

	def test_sweep_expires_old_claims(self):
		old = add_to_date(now_datetime(), seconds=-(claims.GRACE_SECONDS + 5))
		name = fk.claim(self.agent, expires_at=old)
		claims.sweep()
		self.assertEqual(self.status(name), "expired")
		self.assertEqual(self.fake.calls, [])

	def test_sweep_closes_sessions_dialogbrain_ended(self):
		gone = fk.claim(self.agent, session_id="sess-gone")
		failed = fk.claim(self.other, session_id="sess-err")
		self.fake.sessions["sess-err"] = {"status": "error", "error": "device limit"}
		claims.sweep()
		self.assertEqual((self.status(gone), self.status(failed)), ("expired", "failed"))

	def test_sweep_skips_disconnected_site_and_survives_errors(self):
		name = fk.claim(self.agent)
		frappe.db.set_single_value(fk.SETTINGS, "connected", 0, update_modified=False)
		self.assertEqual(claims.sweep(), 0)
		self.assertEqual(self.fake.calls, [])
		frappe.db.set_single_value(fk.SETTINGS, "connected", 1, update_modified=False)
		self.fake.fail[claims.GET] = DialogBrainError("down")
		self.assertEqual(claims.sweep(), 0)
		self.assertEqual(self.status(name), "open")

	def test_tick_runs_the_sweep(self):
		from dialogbrain.sync import reconcile

		with patch("dialogbrain.claims.sweep") as sweep, patch.object(reconcile, "retry_webhook_logs"), patch.object(
			reconcile, "requeue_stale_replies"
		), patch.object(reconcile.frappe, "enqueue"):
			reconcile.tick()
		sweep.assert_called_once()

	# --- the channel shows up (webhook, backfill) before any poll ---

	def test_webhook_first_matches_claim(self):
		name = fk.claim(self.agent)
		self.fake.connect("sess-1", A1)
		upsert_channel(A1, "whatsapp")
		self.assertEqual(fk.allowed(A1), [self.agent])
		self.assertEqual(self.status(name), "connected")

	def test_webhook_first_external_ref(self):
		name = fk.claim(self.agent)
		fk.claim(self.other, session_id="sess-2")
		upsert_channel(A1, "WhatsApp", external_ref=name)
		self.assertEqual(fk.allowed(A1), [self.agent])
		self.assertEqual(self.fake.calls, [])

	def test_webhook_first_fails_closed_then_settles(self):
		mine = fk.claim(self.agent)
		theirs = fk.claim(self.other, session_id="sess-2")
		self.fake.sessions.update({"sess-1": {"status": "scanned"}, "sess-2": {"status": "waiting_scan"}})
		upsert_channel(A1, "whatsapp")
		self.assertEqual(sorted(fk.allowed(A1)), sorted([self.agent, self.other]))
		# the agent's session turns out to be this number; the other one is a different number
		self.fake.connect("sess-1", A1)
		self.fake.connect("sess-2", A2)
		claims.sweep()
		self.assertEqual(fk.allowed(A1), [self.agent])
		self.assertEqual(fk.allowed(A2), [self.other])
		self.assertEqual((self.status(mine), self.status(theirs)), ("connected", "connected"))

	def test_provisional_channel_opens_when_the_claims_end(self):
		name = fk.claim(self.agent)
		self.fake.sessions["sess-1"] = {"status": "waiting_scan"}
		upsert_channel(A1, "whatsapp")  # a number connected elsewhere, not the agent's
		self.assertEqual(fk.allowed(A1), [self.agent])
		claims.close(name, "cancelled")
		self.assertEqual(fk.allowed(A1), [])

	def test_shared_claims_do_not_close_a_channel(self):
		fk.claim(None, requested_by=self.manager)
		self.fake.sessions["sess-1"] = {"status": "waiting_scan"}
		upsert_channel(A1, "whatsapp")
		self.assertEqual(fk.allowed(A1), [])

	def test_no_open_claims_unchanged(self):
		fk.claim(self.agent, channel_type="telegram")
		upsert_channel(A1, "whatsapp")
		self.assertEqual(fk.allowed(A1), [])
		self.assertEqual(self.fake.calls, [])

	def test_existing_channel_never_asks(self):
		upsert_channel(A1, "whatsapp", check_claims=False)
		fk.claim(self.agent)
		upsert_channel(A1, "whatsapp", "New title")
		self.assertEqual(self.fake.calls, [])
		self.assertEqual(fk.allowed(A1), [])

	def test_lookup_bounded(self):
		for i in range(5):
			fk.claim(self.agent, session_id=f"sess-{i}")
		upsert_channel(A1, "whatsapp")
		self.assertEqual(len(self.fake.of(claims.GET)), claims.MAX_LOOKUPS)
		self.assertEqual(fk.allowed(A1), [self.agent])

	def test_lookup_errors_fail_closed(self):
		fk.claim(self.agent)
		self.fake.fail[claims.GET] = RuntimeError("boom")
		upsert_channel(A1, "whatsapp")
		self.assertEqual(fk.allowed(A1), [self.agent])
