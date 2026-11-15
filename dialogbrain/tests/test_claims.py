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

	def test_resolve_existing_shared_channel_is_not_taken_over(self):
		upsert_channel(A1, "whatsapp", check_claims=False)
		name = fk.claim(self.agent)
		result = claims.resolve(name, {"account_id": A1})
		self.assertFalse(result["assigned"])
		self.assertIn("shared channel", result["message"])
		self.assertEqual(fk.allowed(A1), [])  # the other agents keep seeing it
		self.assertEqual(self.status(name), "connected")

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

	def restricted(self, channel):
		return frappe.db.get_value("DB Channel", channel, "restricted")

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

	def test_sweep_asks_before_closing_an_old_claim(self):
		old = add_to_date(now_datetime(), seconds=-(claims.GRACE_SECONDS + 5))
		late = fk.claim(self.agent, expires_at=old)  # connected, but the sweep came late
		dead = fk.claim(self.other, session_id="sess-2", expires_at=old)
		self.fake.connect("sess-1", A1)
		self.fake.sessions["sess-2"] = {"status": "waiting_scan"}
		claims.sweep()
		self.assertEqual((self.status(late), self.status(dead)), ("connected", "expired"))
		self.assertEqual(fk.allowed(A1), [self.agent])

	def test_sweep_closes_sessions_dialogbrain_ended(self):
		gone = fk.claim(self.agent, session_id="sess-gone")
		failed = fk.claim(self.other, session_id="sess-err")
		self.fake.sessions["sess-err"] = {"status": "error", "error": "device limit"}
		claims.sweep()
		self.assertEqual((self.status(gone), self.status(failed)), ("expired", "failed"))

	def test_sweep_keeps_claims_while_dialogbrain_is_down(self):
		old = add_to_date(now_datetime(), seconds=-(claims.GRACE_SECONDS + 5))
		name = fk.claim(self.agent, expires_at=old)
		frappe.db.set_single_value(fk.SETTINGS, "connected", 0, update_modified=False)
		self.assertEqual(claims.sweep(), 0)
		self.assertEqual(self.fake.calls, [])
		frappe.db.set_single_value(fk.SETTINGS, "connected", 1, update_modified=False)
		self.fake.fail[claims.GET] = DialogBrainError("down")
		self.assertEqual(claims.sweep(), 0)
		self.assertEqual(self.status(name), "open")  # no answer: nothing is decided

	def test_sweep_stops_when_dialogbrain_does_not_answer(self):
		for i in range(6):
			fk.claim(self.agent, session_id=f"sess-{i}")
		self.fake.fail[claims.GET] = DialogBrainError("down")
		claims.sweep()
		self.assertEqual(len(self.fake.of(claims.GET)), claims.SWEEP_MAX_ERRORS)

	def test_sweep_one_bad_claim_does_not_stop_the_rest(self):
		fk.claim(self.other, session_id="sess-bad", expires_at=add_to_date(now_datetime(), seconds=10))
		good = fk.claim(self.agent, session_id="sess-1")
		self.fake.connect("sess-bad", A2)
		self.fake.connect("sess-1", A1)
		real = claims.resolve

		def flaky(name, state):
			if state.get("account_id") == A2:
				raise RuntimeError("boom")
			return real(name, state)

		with patch("dialogbrain.claims.resolve", side_effect=flaky), patch("dialogbrain.claims.frappe.log_error") as log:
			self.assertEqual(claims.sweep(), 1)
		log.assert_called_once()
		self.assertEqual(self.status(good), "connected")

	def test_unanswered_start_is_never_confirmed(self):
		old = add_to_date(now_datetime(), seconds=-(claims.GRACE_SECONDS + 5))
		name = fk.claim(self.agent, session_id=None, expires_at=add_to_date(now_datetime(), seconds=60))
		upsert_channel(A1, "whatsapp")  # may be the number of the start that timed out
		self.assertEqual((fk.allowed(A1), self.restricted(A1)), ([], 1))
		frappe.db.set_value(CLAIM, name, "expires_at", old)
		claims.sweep()
		self.assertEqual(self.status(name), "failed")
		self.assertEqual(self.restricted(A1), 1)  # nobody said it is not theirs: managers decide
		self.assertEqual(self.fake.calls, [])

	def test_tick_enqueues_the_sweep_only_when_needed(self):
		with patch("dialogbrain.claims.frappe.enqueue") as enqueue:
			claims.tick()
			enqueue.assert_not_called()
			fk.claim(self.agent)
			claims.tick()
		self.assertEqual(enqueue.call_args.args[0], claims.SWEEP_JOB)
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])

	def test_reconcile_tick_calls_claims_tick(self):
		from dialogbrain.sync import reconcile

		with patch("dialogbrain.claims.tick") as tick, patch.object(reconcile, "retry_webhook_logs"), patch.object(
			reconcile, "requeue_stale_replies"
		), patch.object(reconcile.frappe, "enqueue"):
			reconcile.tick()
		tick.assert_called_once()

	# --- the channel shows up (webhook, backfill) before any poll ---

	def test_webhook_first_is_managers_only_until_settled(self):
		name = fk.claim(self.agent)
		self.fake.connect("sess-1", A1)
		upsert_channel(A1, "whatsapp")
		self.assertEqual(self.fake.calls, [])  # no DialogBrain call while a webhook is processed
		self.assertEqual((fk.allowed(A1), self.restricted(A1)), ([], 1))
		for user, sees in ((self.agent, False), (self.other, False)):
			from dialogbrain.permissions import allowed_channels

			self.assertEqual(A1 in allowed_channels(user), sees)
		claims.sweep()
		self.assertEqual((fk.allowed(A1), self.restricted(A1)), ([self.agent], 0))
		self.assertEqual(self.status(name), "connected")

	def test_webhook_first_external_ref(self):
		name = fk.claim(self.agent)
		fk.claim(self.other, session_id="sess-2")
		upsert_channel(A1, "WhatsApp", external_ref=name)
		self.assertEqual((fk.allowed(A1), self.restricted(A1)), ([self.agent], 0))
		self.assertEqual(self.fake.calls, [])
		self.assertEqual(self.status(name), "connected")

	def test_two_owners_never_see_each_others_number(self):
		from dialogbrain.permissions import allowed_channels, permitted_users

		mine = fk.claim(self.agent)
		theirs = fk.claim(self.other, session_id="sess-2")
		upsert_channel(A1, "whatsapp")
		upsert_channel(A2, "whatsapp")
		for user in (self.agent, self.other):
			self.assertFalse({A1, A2} & set(allowed_channels(user)))
		self.assertNotIn(self.agent, permitted_users(A1))
		self.assertIn(self.manager, permitted_users(A1))
		self.fake.connect("sess-1", A1)
		self.fake.connect("sess-2", A2)
		claims.sweep()
		self.assertEqual((fk.allowed(A1), fk.allowed(A2)), ([self.agent], [self.other]))
		self.assertEqual((self.restricted(A1), self.restricted(A2)), (0, 0))
		self.assertEqual((self.status(mine), self.status(theirs)), ("connected", "connected"))
		self.assertNotIn(A1, allowed_channels(self.other))

	def test_number_connected_elsewhere_opens_once_dialogbrain_confirms(self):
		mine = fk.claim(self.agent)
		theirs = fk.claim(self.other, session_id="sess-2")
		self.fake.sessions.update({"sess-1": {"status": "waiting_scan"}, "sess-2": {"status": "waiting_scan"}})
		upsert_channel(A1, "whatsapp")  # connected in DialogBrain itself, nobody's claim
		self.fake.sessions["sess-1"] = {"status": "expired"}
		claims.sweep()
		self.assertEqual(self.restricted(A1), 1)  # the other claim may still be it
		self.fake.sessions["sess-2"] = {"status": "expired"}
		claims.sweep()
		self.assertEqual((self.restricted(A1), fk.allowed(A1)), (0, []))
		self.assertEqual((self.status(mine), self.status(theirs)), ("expired", "expired"))

	def test_unconfirmed_close_keeps_the_channel_for_managers(self):
		name = fk.claim(self.agent)
		upsert_channel(A1, "whatsapp")
		claims.close(name, "cancelled")  # e.g. given up while DialogBrain was unreachable
		self.assertEqual((self.restricted(A1), fk.allowed(A1)), (1, []))
		later = fk.claim(self.other, session_id="sess-2")
		upsert_channel(A2, "whatsapp")
		self.fake.sessions["sess-2"] = {"status": "expired"}
		claims.sweep()
		self.assertEqual(self.status(later), "expired")
		self.assertEqual((self.restricted(A1), self.restricted(A2)), (1, 0))

	def test_shared_claims_do_not_close_a_channel(self):
		fk.claim(None, requested_by=self.manager)
		upsert_channel(A1, "whatsapp")
		self.assertEqual((fk.allowed(A1), self.restricted(A1)), ([], 0))

	def test_no_open_claims_unchanged(self):
		fk.claim(self.agent, channel_type="telegram")
		upsert_channel(A1, "whatsapp")
		self.assertEqual((fk.allowed(A1), self.restricted(A1)), ([], 0))
		self.assertEqual(self.fake.calls, [])

	def test_existing_channel_never_asks(self):
		upsert_channel(A1, "whatsapp", check_claims=False)
		fk.claim(self.agent)
		upsert_channel(A1, "whatsapp", "New title")
		self.assertEqual(self.fake.calls, [])
		self.assertEqual((fk.allowed(A1), self.restricted(A1)), ([], 0))

	def test_new_channel_starts_the_sweep(self):
		fk.claim(self.agent)
		with patch("dialogbrain.claims.frappe.enqueue") as enqueue:
			upsert_channel(A1, "whatsapp")
		self.assertEqual(enqueue.call_args.args[0], claims.SWEEP_JOB)
