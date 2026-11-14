import copy
import re
from datetime import timedelta, timezone
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_datetime, get_system_timezone, now_datetime

from dialogbrain.client import DialogBrainClient, DialogBrainError
from dialogbrain.tests.utils import DialogBrainTestCase, load_fixture

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SETTINGS = "DialogBrain Settings"
UNCONFIRMED_ERROR = "Not confirmed by DialogBrain — check the chat before resending"


# --- fixture-shaped fakes -----------------------------------------------------


def _utc(dt):
	"""System-time naive datetime -> aware UTC."""
	return dt.replace(tzinfo=ZoneInfo(get_system_timezone())).astimezone(timezone.utc)


def _hist_ts(dt):
	"""read_history timestamp format: ``2026-11-13 12:06:51.597772+00:00``."""
	return str(_utc(dt))


def _utc_date(dt):
	return _utc(dt).date().isoformat()


def _entry(tid, account="155", channel="telegram", kind="dm", title=None, ref=None):
	entry = copy.deepcopy(load_fixture("search_threads")["data"]["threads"][0])
	entry.update(
		{
			"id": tid,
			"thread_id": tid,
			"title": title or f"Contact {tid}",
			"channel": channel,
			"kind": kind,
			"channel_account_id": account,
			"channel_ref": ref or f"{channel}:{tid}00",
		}
	)
	entry["account"] = dict(entry["account"], id=int(account), channel=channel)
	return entry


def _hmsg(mid, dt, text, out=False, sender=None):
	msg = copy.deepcopy(load_fixture("read_history_full")["data"]["messages"][1 if out else 0])
	msg.update({"id": mid, "sent_at": _hist_ts(dt), "text": text, "is_outgoing": out})
	if sender is not None:
		msg["sender"] = sender
	return msg


class FakeDialogBrain:
	"""search.threads + messages.read_history with DialogBrain's paging semantics.

	``page_cap`` / ``history_cap`` emulate small server pages so paging is exercised while
	the caller still asks for the documented limits (50 / 100).
	"""

	def __init__(self, threads, messages, page_cap=50, history_cap=100, fail_history=(), webhooks=None):
		self.threads = threads  # server order
		self.meta = {t["id"]: t for t in threads}
		self.messages = messages
		self.page_cap = page_cap
		self.history_cap = history_cap
		self.fail_history = set(fail_history)
		self.calls = []
		self.webhooks = webhooks if webhooks is not None else [{"id": 9, "active": True}]
		self.activated = []

	def _last(self, tid):
		msgs = self.messages.get(tid) or []
		return max((get_datetime(m["sent_at"]) for m in msgs), default=None)

	def execute(self, tool_id, params, thread_id=None):
		self.calls.append((tool_id, dict(params)))
		if tool_id == "search.threads":
			since = params.get("since")
			items = [
				t
				for t in self.threads
				if self._last(t["id"]) and (not since or self._last(t["id"]).date().isoformat() >= since)
			]
			offset = params.get("offset") or 0
			page = items[offset : offset + min(params.get("limit") or 10, self.page_cap)]
			return {
				"goal": "threads",
				"mode": "list",
				"threads": copy.deepcopy(page),
				"count": len(page),
				"total_count": len(items),
				"offset": offset,
				"has_more": offset + len(page) < len(items),
			}
		if tool_id == "messages.read_history":
			tid = str(params["thread_id"])
			if tid in self.fail_history:
				raise DialogBrainError("read_history failed")
			msgs = sorted(self.messages.get(tid) or [], key=lambda m: get_datetime(m["sent_at"]))
			if params.get("date_from"):
				msgs = [m for m in msgs if get_datetime(m["sent_at"]).date().isoformat() >= params["date_from"]]
			newest_first = list(reversed(msgs))
			offset = params.get("offset") or 0
			page = newest_first[offset : offset + min(params.get("limit") or 10, self.history_cap)]
			has_more = offset + len(page) < len(newest_first)
			meta = self.meta.get(tid) or {"title": f"Contact {tid}", "channel": "telegram", "channel_account_id": "155"}
			last = self._last(tid)
			return {
				"thread": {
					"id": int(tid),
					"title": meta["title"],
					"channel": meta["channel"],
					"last_message_ts": str(last) if last else None,
					"account_id": int(meta["channel_account_id"]),
					"synced": True,
				},
				"messages": copy.deepcopy(list(reversed(page))),
				"total_returned": len(page),
				"offset": offset,
				"has_more": has_more,
				"next_offset": offset + len(page) if has_more else None,
			}
		raise AssertionError(f"unexpected tool {tool_id}")

	def list_webhooks(self):
		self.calls.append(("list_webhooks", {}))
		return copy.deepcopy(self.webhooks)

	def activate_webhook(self, webhook_id):
		self.activated.append(str(webhook_id))

	# helpers for assertions
	def tool_calls(self, tool_id):
		return [p for t, p in self.calls if t == tool_id]

	def history_calls(self, tid):
		return [p for p in self.tool_calls("messages.read_history") if str(p["thread_id"]) == tid]


# --- tests --------------------------------------------------------------------


class TestBackfillReconcile(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for dt in (
			"DB Message Attachment",
			"DB Message",
			"DB Draft",
			"DB Conversation",
			"DB Channel User",
			"DB Channel",
			"DB Webhook Log",
		):
			frappe.db.delete(dt)
		self.now = now_datetime()
		self._settings(
			connected=1,
			network_mode="public",
			last_reconcile_at=self.now - timedelta(minutes=10),
			backfill_days=30,
			backfill_status="done",
			webhook_id="9",
			webhook_status=None,
		)

	def _settings(self, **values):
		for key, value in values.items():
			frappe.db.set_single_value(SETTINGS, key, value)

	def _get(self, key):
		return frappe.db.get_single_value(SETTINGS, key)

	def _run(self, fn, fake, *args, **kwargs):
		with patch.object(DialogBrainClient, "from_settings", return_value=fake):
			return fn(*args, **kwargs)

	def _count(self, conversation=None):
		filters = {"conversation": conversation} if conversation else {}
		return frappe.db.count("DB Message", filters)

	def _conv(self, tid):
		return frappe.db.get_value("DB Conversation", {"thread_id": tid})

	def _store(self, tid, messages, entry=None):
		"""Mirror ``messages`` of thread ``tid`` as if delivered earlier (silently)."""
		from dialogbrain.sync.backfill import history_message_to_event
		from dialogbrain.sync.upsert import apply_message_counters, upsert_channel, upsert_conversation, upsert_message

		entry = entry or _entry(tid)
		channel = upsert_channel(entry["channel_account_id"], entry["channel"])
		conv, _ = upsert_conversation({"id": tid, "channel_ref": entry["channel_ref"], "title": entry["title"]}, channel)
		for msg in messages:
			name, created = upsert_message(conv, history_message_to_event(msg), silent=True)
			if created:
				apply_message_counters(conv, name, silent=True)
		return conv

	# --- mapper (R12) -------------------------------------------------------

	def test_history_message_to_event_against_fixture(self):
		from dialogbrain.sync.backfill import history_message_to_event
		from dialogbrain.sync.upsert import to_system_datetime

		fixture = load_fixture("read_history_full")["data"]
		conv = self._store("8451249", fixture["messages"], _entry("8451249", "201", "livechat", "livechat"))
		for msg in fixture["messages"]:
			event = history_message_to_event(msg)
			self.assertEqual(event["db_id"], str(msg["id"]))
			self.assertEqual(event["html"], msg["text"])
			self.assertEqual(event["is_outgoing"], msg["is_outgoing"])
			row = frappe.get_doc("DB Message", {"db_id": str(msg["id"])})
			self.assertEqual(row.conversation, conv)
			self.assertEqual(row.text, msg["text"])  # full body (184 chars for the long one)
			self.assertEqual(get_datetime(row.sent_at), to_system_datetime(msg["sent_at"]))
			self.assertEqual(row.direction, "out" if msg["is_outgoing"] else "in")
			self.assertEqual(row.author_type, "manager" if msg["is_outgoing"] else "customer")
			self.assertFalse(row.sent_by)
		self.assertEqual(len(frappe.db.get_value("DB Message", {"db_id": "27771418"}, "text")), 184)

	def test_history_outgoing_authorship(self):
		t = self.now - timedelta(hours=1)
		self._store(
			"1001",
			[
				_hmsg(1, t, "agent", out=True, sender={"id": "system", "name": "Sales Bot", "username": None}),
				_hmsg(2, t, "own", out=True, sender={"id": None, "name": "You", "username": None}),
				_hmsg(3, t, "bare", out=True, sender={}),
			],
		)
		rows = {r.db_id: r for r in frappe.get_all("DB Message", fields=["db_id", "author_type", "sent_by"])}
		self.assertEqual(rows["1"].author_type, "agent")
		self.assertEqual(rows["2"].author_type, "manager")
		self.assertEqual(rows["3"].author_type, "manager")
		self.assertFalse(rows["2"].sent_by or rows["3"].sent_by)

	# --- backfill -----------------------------------------------------------

	def _backfill_fake(self, **kwargs):
		t = self.now - timedelta(days=2)
		threads = [_entry("1001"), _entry("1002", kind="group"), _entry("8451249", "201", "livechat", "livechat")]
		messages = {
			"1001": [_hmsg(100 + i, t + timedelta(minutes=i), f"a{i}", out=bool(i % 2)) for i in range(5)]
			+ [_hmsg(99, self.now - timedelta(days=40), "too old")],
			"1002": [_hmsg(200, t, "group hello")],
			"8451249": [_hmsg(300 + i, t + timedelta(hours=i), f"c{i}") for i in range(3)],
		}
		return FakeDialogBrain(threads, messages, **kwargs)

	def test_backfill_paginates_threads_and_messages(self):
		from dialogbrain.sync.backfill import run_backfill

		fake = self._backfill_fake(page_cap=2, history_cap=2)
		self._run(run_backfill, fake)

		searches = fake.tool_calls("search.threads")
		self.assertEqual([p["offset"] for p in searches], [0, 2])
		since = searches[0]["since"]
		self.assertRegex(since, DATE_RE)
		self.assertEqual(since, _utc_date(self.now - timedelta(days=30)))
		self.assertTrue(all(p["limit"] == 50 and p["since"] == since for p in searches))

		history = fake.history_calls("1001")
		self.assertEqual([p["offset"] for p in history], [0, 2, 4])
		self.assertTrue(all(p["limit"] == 100 and p["date_from"] == since for p in history))

		self.assertEqual(self._count(), 9)  # the 40-day-old message is outside the window
		self.assertFalse(frappe.db.exists("DB Message", {"db_id": "99"}))
		conv = self._conv("1001")
		self.assertEqual(frappe.db.get_value("DB Conversation", conv, "thread_ref"), "telegram:100100")
		last = frappe.db.get_value("DB Conversation", conv, ["last_message_at", "last_message_preview"], as_dict=True)
		self.assertEqual(last.last_message_preview, "a4")
		self.assertEqual(self._get("backfill_status"), "done")

	def test_backfill_links_whatsapp_customer_by_phone(self):
		"""F2: backfill/reconcile get no contact metadata; the phone of a WhatsApp DM is its
		ref. The existing Customer is linked (verified: full number on a phone channel, R8/R9);
		a group is not; an email DM is linked by address, unverified."""
		from dialogbrain.sync.backfill import run_backfill
		from dialogbrain.tests.test_party import _customer

		digits = "99" + frappe.generate_hash(length=10).translate(str.maketrans("abcdef", "123456"))[:10]
		email = f"f2-{frappe.generate_hash(length=6)}@example.com"
		customer = _customer(f"F2 WA {digits}", phone=f"+{digits[:2]} {digits[2:6]} {digits[6:]}")
		mail_customer = _customer(f"F2 Mail {email}", email=email.upper())
		now = self.now - timedelta(hours=1)
		entries = [
			_entry("3001", account="701", channel="whatsapp", ref=f"whatsapp:{digits}@s.whatsapp.net"),
			_entry("3002", account="701", channel="whatsapp", kind="group", ref=f"whatsapp:{digits}-1@g.us"),
			_entry("3003", account="702", channel="email", ref=f"email:dm:{email}"),
		]
		messages = {e["id"]: [_hmsg(int(e["id"]) * 10, now, "hello")] for e in entries}
		self._run(run_backfill, FakeDialogBrain(entries, messages))

		dm = frappe.db.get_value(
			"DB Conversation", self._conv("3001"), ["phone", "party_type", "party", "link_source"], as_dict=True
		)
		self.assertEqual(dm.phone, f"+{digits}")
		self.assertEqual((dm.party_type, dm.party, dm.link_source), ("Customer", customer, "auto_verified"))
		group = frappe.db.get_value("DB Conversation", self._conv("3002"), ["phone", "party"], as_dict=True)
		self.assertEqual((group.phone, group.party), (None, None))
		mail = frappe.db.get_value(
			"DB Conversation", self._conv("3003"), ["email", "party", "link_source"], as_dict=True
		)
		self.assertEqual((mail.email, mail.party, mail.link_source), (email, mail_customer, "auto_unverified"))

	def test_reconcile_fills_phone_of_known_conversation(self):
		"""F2: conversations mirrored before the fix get their phone (and party) on the next
		sync of the thread."""
		from dialogbrain.sync.reconcile import reconcile
		from dialogbrain.tests.test_party import _customer

		digits = "98" + frappe.generate_hash(length=10).translate(str.maketrans("abcdef", "123456"))[:10]
		customer = _customer(f"F2 R {digits}", phone=f"+{digits}")
		entry = _entry("3101", account="701", channel="whatsapp", ref=f"whatsapp:{digits}@s.whatsapp.net")
		old = [_hmsg(31010, self.now - timedelta(hours=2), "old")]
		conv = self._store("3101", old, entry=entry)
		frappe.db.set_value("DB Conversation", conv, {"phone": None, "party_type": None, "party": None, "link_source": None})
		new = old + [_hmsg(31011, self.now - timedelta(minutes=1), "new")]
		self._run(reconcile, FakeDialogBrain([entry], {"3101": new}))
		row = frappe.db.get_value("DB Conversation", conv, ["phone", "party", "link_source"], as_dict=True)
		self.assertEqual((row.phone, row.party, row.link_source), (f"+{digits}", customer, "auto_verified"))

	def test_backfill_survives_deadlock_that_ended_the_transaction(self):
		"""Seen live: a deadlock with the webhook processor rolled back the whole
		transaction, so ``rollback to savepoint`` itself failed and aborted the backfill.
		The thread is retried once; the backfill goes on."""
		from dialogbrain.sync import backfill

		fake = self._backfill_fake()
		real_import = backfill.import_thread
		calls = []

		def flaky(client, thread_id, *args, **kwargs):
			calls.append(thread_id)
			if calls.count(thread_id) == 1 and thread_id == "1001":
				raise frappe.QueryDeadlockError("Deadlock found")
			return real_import(client, thread_id, *args, **kwargs)

		def rollback(*args, save_point=None, **kwargs):
			if save_point:
				raise Exception("(1305, 'SAVEPOINT dialogbrain_backfill does not exist')")
			# a full rollback here would undo the test's own fixtures: nothing to undo

		with patch.object(backfill, "import_thread", side_effect=flaky), patch.object(
			frappe.db, "rollback", side_effect=rollback
		):
			self._run(backfill.run_backfill, fake)
		self.assertEqual(calls.count("1001"), 2)
		self.assertTrue(self._conv("1001"))
		self.assertEqual(self._get("backfill_status"), "done")

	def test_backfill_is_silent(self):
		from dialogbrain.sync.backfill import run_backfill

		before = frappe.db.count("Notification Log")
		with patch("dialogbrain.sync.upsert.publish") as publish, patch("dialogbrain.sync.upsert.notify") as notify:
			self._run(run_backfill, self._backfill_fake())
		publish.assert_not_called()
		notify.assert_not_called()
		self.assertEqual(frappe.db.count("Notification Log"), before)
		self.assertEqual(self._count(), 9)

	def test_backfill_rerun_no_duplicates(self):
		from dialogbrain.sync.backfill import run_backfill

		self._run(run_backfill, self._backfill_fake())
		first = self._count()
		conversations = frappe.db.count("DB Conversation")
		self._run(run_backfill, self._backfill_fake(page_cap=1, history_cap=1))
		self.assertEqual(self._count(), first)
		self.assertEqual(frappe.db.count("DB Conversation"), conversations)

	def test_backfill_error_status(self):
		from dialogbrain.sync.backfill import run_backfill

		fake = MagicMock()
		fake.execute.side_effect = DialogBrainError("DialogBrain HTTP 500: boom")
		self._run(run_backfill, fake)
		self.assertTrue(self._get("backfill_status").startswith("error: "))
		self.assertIn("boom", self._get("backfill_status"))

	# --- reconcile ----------------------------------------------------------

	def test_reconcile_fetches_missed_messages(self):
		from dialogbrain.sync.reconcile import reconcile

		t = self.now - timedelta(hours=3)
		m1 = _hmsg(1, t, "first")
		m2 = _hmsg(2, self.now - timedelta(minutes=2), "missed while webhooks were down")
		conv = self._store("1001", [m1])
		fake = FakeDialogBrain([_entry("1001")], {"1001": [m1, m2]})
		start = now_datetime()
		with patch("dialogbrain.sync.upsert.notify") as notify:
			self._run(reconcile, fake)

		self.assertTrue(frappe.db.exists("DB Message", {"db_id": "2", "conversation": conv}))
		notify.assert_called_with(conv, "inbound")  # reconcile is not silent
		syncs = [p for p in fake.history_calls("1001") if p.get("date_from")]
		self.assertEqual(syncs[0]["date_from"], _utc_date(t))
		self.assertEqual(syncs[0]["limit"], 100)
		self.assertGreaterEqual(get_datetime(self._get("last_reconcile_at")), start - timedelta(seconds=1))

	def test_reconcile_skips_unchanged_threads(self):
		from dialogbrain.sync.reconcile import PROBE_LIMIT, reconcile

		msgs = [_hmsg(1, self.now - timedelta(hours=2), "a"), _hmsg(2, self.now - timedelta(hours=1), "b")]
		self._store("1001", msgs)
		fake = FakeDialogBrain([_entry("1001")], {"1001": msgs})
		with patch("dialogbrain.sync.upsert.publish") as publish:
			self._run(reconcile, fake)
		calls = fake.history_calls("1001")
		self.assertEqual(len(calls), 1)  # the probe only
		self.assertEqual(calls[0]["limit"], PROBE_LIMIT)
		self.assertNotIn("date_from", calls[0])
		publish.assert_not_called()
		self.assertEqual(self._count(), 2)

	def test_reconcile_checks_every_thread_no_early_stop(self):
		"""search.threads puts pinned threads first and does not say which are pinned, so an
		unchanged thread does not end the pass."""
		from dialogbrain.sync.reconcile import reconcile

		old = [_hmsg(1, self.now - timedelta(hours=5), "pinned, unchanged")]
		self._store("1001", old)
		new = [_hmsg(2, self.now - timedelta(minutes=1), "newer, below the pinned one")]
		fake = FakeDialogBrain([_entry("1001"), _entry("1002")], {"1001": old, "1002": new})
		self._run(reconcile, fake)
		self.assertTrue(frappe.db.exists("DB Message", {"db_id": "2"}))
		self.assertEqual(frappe.db.get_value("DB Conversation", self._conv("1002"), "thread_ref"), "telegram:100200")

	def test_reconcile_detects_missed_older_message(self):
		"""A later message arrived (webhook / own send) after a missed one: the newest stored
		timestamp equals DialogBrain's, but the probe ids reveal the gap."""
		from dialogbrain.sync.reconcile import reconcile

		m1 = _hmsg(1, self.now - timedelta(minutes=30), "q1")
		m2 = _hmsg(2, self.now - timedelta(minutes=20), "q2 (missed)")
		m3 = _hmsg(3, self.now - timedelta(minutes=10), "our reply", out=True)
		self._store("1001", [m1, m3])
		fake = FakeDialogBrain([_entry("1001")], {"1001": [m1, m2, m3]})
		self._run(reconcile, fake)
		self.assertTrue(frappe.db.exists("DB Message", {"db_id": "2"}))

	def test_reconcile_cursor_not_advanced_on_global_error(self):
		"""search.threads or the webhook check failing keeps the cursor (R13)."""
		from dialogbrain.sync.reconcile import reconcile

		before = get_datetime(self._get("last_reconcile_at"))
		fake = MagicMock()
		fake.list_webhooks.return_value = [{"id": 9, "active": True}]
		fake.execute.side_effect = DialogBrainError("DialogBrain HTTP 500")
		self._run(reconcile, fake)
		self.assertEqual(get_datetime(self._get("last_reconcile_at")), before)

		webhook_down = FakeDialogBrain([], {})
		webhook_down.list_webhooks = MagicMock(side_effect=DialogBrainError("HTTP 502"))
		self._run(reconcile, webhook_down)
		self.assertEqual(get_datetime(self._get("last_reconcile_at")), before)
		self.assertIn("check failed", self._get("webhook_status"))

	def _failures(self):
		return frappe.parse_json(self._get("reconcile_failures") or "{}")

	def test_failing_thread_does_not_block_cursor_and_is_retried(self):
		from dialogbrain.sync.reconcile import reconcile

		before = get_datetime(self._get("last_reconcile_at"))
		fake = FakeDialogBrain(
			[_entry("1001"), _entry("1002")],
			{"1001": [_hmsg(1, self.now - timedelta(minutes=3), "x")], "1002": [_hmsg(2, self.now - timedelta(minutes=2), "y")]},
			fail_history={"1001"},
		)
		self._run(reconcile, fake)
		self.assertGreater(get_datetime(self._get("last_reconcile_at")), before)  # R13
		self.assertTrue(frappe.db.exists("DB Message", {"db_id": "2"}))
		record = self._failures()["1001"]
		since = fake.tool_calls("search.threads")[0]["since"]
		self.assertEqual((record["count"], record["date_from"]), (1, since))
		self.assertIn("read_history failed", record["error"])
		self.assertIn("1001", self._get("reconcile_status"))

		# next pass: the thread is no longer in the search window but is retried from its date
		fake.threads, fake.fail_history, fake.calls = [], set(), []
		self._run(reconcile, fake)
		self.assertEqual([p.get("date_from") for p in fake.history_calls("1001")], [since])
		self.assertTrue(frappe.db.exists("DB Message", {"db_id": "1"}))
		self.assertEqual(self._failures(), {})
		self.assertEqual(self._get("reconcile_status"), "OK")
		self.assertEqual(frappe.db.get_value("DB Conversation", self._conv("1001"), "thread_ref"), "telegram:100100")

	def test_thread_skipped_after_5_failures_until_resync(self):
		from dialogbrain.sync.backfill import enqueue_backfill
		from dialogbrain.sync.reconcile import reconcile

		fake = FakeDialogBrain([_entry("1001")], {"1001": [_hmsg(1, self.now - timedelta(minutes=3), "x")]}, fail_history={"1001"})
		for _ in range(5):
			self._run(reconcile, fake)
		self.assertEqual(self._failures()["1001"]["count"], 5)
		fake.calls = []
		self._run(reconcile, fake)
		self.assertEqual(fake.history_calls("1001"), [])  # skipped, even though it is in the window
		self.assertEqual(self._failures()["1001"]["count"], 5)
		status = self._get("reconcile_status")
		self.assertIn("1001", status)
		self.assertIn("skipped", status)

		with patch("frappe.enqueue"):
			enqueue_backfill()  # manual Re-sync
		self.assertEqual(self._failures(), {})
		self.assertFalse(self._get("reconcile_status"))

	def test_since_clamped_to_backfill_days(self):
		from dialogbrain.sync.reconcile import reconcile

		self._settings(last_reconcile_at=self.now - timedelta(days=60), backfill_days=30)
		fake = FakeDialogBrain([], {})
		self._run(reconcile, fake)
		self.assertEqual(fake.tool_calls("search.threads")[0]["since"], _utc_date(self.now - timedelta(days=30)))

	def test_reconcile_after_long_outage_uses_older_since(self):
		from dialogbrain.sync.reconcile import reconcile

		for last in (self.now - timedelta(minutes=10), self.now - timedelta(days=10)):
			self._settings(last_reconcile_at=last)
			fake = FakeDialogBrain([], {})
			self._run(reconcile, fake)
			since = fake.tool_calls("search.threads")[0]["since"]
			self.assertRegex(since, DATE_RE)
			self.assertEqual(since, _utc_date(last - timedelta(days=1)))
			self.assertEqual(fake.tool_calls("search.threads")[0]["limit"], 50)

	def test_reconcile_pages_search(self):
		from dialogbrain.sync.reconcile import reconcile

		t = self.now - timedelta(minutes=5)
		threads = [_entry(str(1000 + i)) for i in range(5)]
		fake = FakeDialogBrain(threads, {e["id"]: [_hmsg(i, t, "hi")] for i, e in enumerate(threads)}, page_cap=2)
		self._run(reconcile, fake)
		self.assertEqual([p["offset"] for p in fake.tool_calls("search.threads")], [0, 2, 4])
		self.assertEqual(frappe.db.count("DB Conversation"), 5)

	def test_reconcile_reactivates_paused_webhook(self):
		from dialogbrain.sync.reconcile import reconcile

		msgs = [_hmsg(1, self.now - timedelta(hours=1), "known")]
		self._store("1001", msgs)

		active = FakeDialogBrain([_entry("1001")], {"1001": msgs})
		self._run(reconcile, active)
		self.assertEqual(active.activated, [])

		paused = FakeDialogBrain([_entry("1001")], {"1001": msgs}, webhooks=[])  # list shows active only
		self._run(reconcile, paused)
		self.assertEqual(paused.activated, ["9"])
		self.assertIn("reactivated", self._get("webhook_status"))
		# events were not delivered while paused: every thread of the window is re-read
		self.assertTrue(any(p.get("date_from") for p in paused.history_calls("1001")))

		self._settings(network_mode="private")
		private = FakeDialogBrain([_entry("1001")], {"1001": msgs}, webhooks=[])
		self._run(reconcile, private)
		self.assertEqual(private.tool_calls("list_webhooks"), [])
		self.assertEqual(private.activated, [])

	def test_deleted_webhook_does_not_freeze_cursor(self):
		"""F9: activate 404 = the subscription is gone for good. Clear it, tell the admin to
		Reconnect, and let the cursor advance (no endless deep re-reads)."""
		from frappe.utils.password import get_decrypted_password

		from dialogbrain.sync.reconcile import WEBHOOK_MISSING_STATUS, reconcile

		frappe.db.set_single_value(SETTINGS, "webhook_secret", "s3cret")
		from frappe.utils.password import set_encrypted_password

		set_encrypted_password(SETTINGS, SETTINGS, "s3cret", "webhook_secret")
		msgs = [_hmsg(1, self.now - timedelta(hours=1), "known")]
		self._store("1001", msgs)
		before = get_datetime(self._get("last_reconcile_at"))

		gone = FakeDialogBrain([_entry("1001")], {"1001": msgs}, webhooks=[])
		gone.activate_webhook = MagicMock(side_effect=DialogBrainError("HTTP 404: not found", status=404))
		self._run(reconcile, gone)

		self.assertGreater(get_datetime(self._get("last_reconcile_at")), before)
		self.assertFalse(self._get("webhook_id"))
		self.assertFalse(get_decrypted_password(SETTINGS, SETTINGS, "webhook_secret", raise_exception=False))
		self.assertEqual(self._get("webhook_status"), WEBHOOK_MISSING_STATUS)
		self.assertIn("Reconnect", WEBHOOK_MISSING_STATUS)
		# the pass that found it missing re-reads the window (events were lost)
		self.assertTrue(any(p.get("date_from") for p in gone.history_calls("1001")))

		# next pass: no webhook check at all, the status stays visible
		again = FakeDialogBrain([_entry("1001")], {"1001": msgs}, webhooks=[])
		self._run(reconcile, again)
		self.assertEqual(again.tool_calls("list_webhooks"), [])
		self.assertEqual(self._get("webhook_status"), WEBHOOK_MISSING_STATUS)

	def test_webhook_activate_other_error_keeps_cursor(self):
		"""Only a 404 means "deleted"; any other activate error keeps the cursor (R13)."""
		from dialogbrain.sync.reconcile import reconcile

		before = get_datetime(self._get("last_reconcile_at"))
		down = FakeDialogBrain([], {}, webhooks=[])
		down.activate_webhook = MagicMock(side_effect=DialogBrainError("HTTP 503", status=503))
		self._run(reconcile, down)
		self.assertEqual(get_datetime(self._get("last_reconcile_at")), before)
		self.assertEqual(self._get("webhook_id"), "9")

	def test_reconcile_waits_for_backfill(self):
		from dialogbrain.sync.reconcile import reconcile

		self._settings(backfill_status="running 3/10")
		before = get_datetime(self._get("last_reconcile_at"))
		fake = FakeDialogBrain([_entry("1001")], {"1001": [_hmsg(1, self.now, "x")]})
		self._run(reconcile, fake)
		self.assertEqual(fake.calls, [])
		self.assertEqual(get_datetime(self._get("last_reconcile_at")), before)

	# --- R11: unconfirmed replies -------------------------------------------

	def _reply(self, conv, text, minutes_ago, status="unknown"):
		return (
			frappe.get_doc(
				{
					"doctype": "DB Message",
					"conversation": conv,
					"direction": "out",
					"author_type": "manager",
					"sent_by": "Administrator",
					"text": text,
					"sent_at": self.now - timedelta(minutes=minutes_ago),
					"delivery_status": status,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def test_reconcile_fails_unconfirmed_unknown_replies(self):
		from dialogbrain.sync.reconcile import reconcile

		# conversation whose thread has no activity in the window (the send never arrived)
		quiet = [_hmsg(1, self.now - timedelta(days=5), "old")]
		conv_a = self._store("1001", quiet)
		lost = self._reply(conv_a, "Did this arrive?", 20)
		recent = self._reply(conv_a, "Just sent", 2)

		# conversation where the send did arrive (history has the echo)
		conv_b = self._store("1002", [_hmsg(2, self.now - timedelta(hours=1), "question")])
		arrived = self._reply(conv_b, "Yes, it is in stock", 15)
		echo = _hmsg(3, self.now - timedelta(minutes=15), "Yes, it is in stock", out=True)

		fake = FakeDialogBrain(
			[_entry("1002"), _entry("1001")],
			{"1001": quiet, "1002": [_hmsg(2, self.now - timedelta(hours=1), "question"), echo]},
		)
		self._run(reconcile, fake)

		row = frappe.db.get_value("DB Message", lost, ["delivery_status", "error", "db_id"], as_dict=True)
		self.assertEqual(row.delivery_status, "failed")
		self.assertEqual(row.error, UNCONFIRMED_ERROR)
		self.assertFalse(row.db_id)
		self.assertEqual(frappe.db.get_value("DB Message", recent, "delivery_status"), "unknown")
		row = frappe.db.get_value("DB Message", arrived, ["delivery_status", "db_id", "sent_by"], as_dict=True)
		self.assertEqual((row.delivery_status, row.db_id, row.sent_by), ("sent", "3", "Administrator"))
		self.assertEqual(self._count(conv_b), 2)  # the echo did not create a second row
		# the quiet thread was read back to the date of the unconfirmed reply
		self.assertTrue(any(p.get("date_from") for p in fake.history_calls("1001")))

	def test_sweep_leaves_a_just_retried_reply_alone(self):
		"""B4 fix round 1: a retried reply keeps its old sent_at; its last attempt (raw.send)
		is what counts, so the sweep does not fail it while the retry may be in flight."""
		import json

		from dialogbrain.sync.reconcile import reconcile

		quiet = [_hmsg(1, self.now - timedelta(days=5), "old")]
		conv = self._store("1001", quiet)
		retried = self._reply(conv, "Did this arrive?", 30)
		old = self._reply(conv, "Old attempt", 30)
		for name, attempt in ((retried, self.now - timedelta(minutes=1)), (old, self.now - timedelta(minutes=20))):
			raw = {"send": {"request_id": name, "params": {}, "attempt_at": str(attempt),
							"first_attempt_at": str(self.now - timedelta(minutes=30))}}
			frappe.db.set_value("DB Message", name, "raw", json.dumps(raw))
		fake = FakeDialogBrain([_entry("1001")], {"1001": quiet})
		self._run(reconcile, fake)
		self.assertEqual(frappe.db.get_value("DB Message", retried, "delivery_status"), "unknown")
		self.assertEqual(frappe.db.get_value("DB Message", old, "delivery_status"), "failed")

	def test_fail_unconfirmed_rechecks_the_attempt_under_the_lock(self):
		"""B4 fix round 2: a row selected as unconfirmed but claimed again before the lock
		(fresh claimed_at) is left alone."""
		import json

		from dialogbrain.sync import reconcile as rec

		conv = self._store("1001", [_hmsg(1, self.now - timedelta(days=5), "old")])
		name = self._reply(conv, "Retried", 30)
		frappe.db.set_value("DB Message", name, "raw", json.dumps({"claimed_at": str(self.now)}))
		cutoff = self.now - timedelta(minutes=10)
		with patch.object(rec, "_unconfirmed", return_value=[(name, conv, self.now - timedelta(minutes=30))]):
			rec._fail_unconfirmed(conv, cutoff)
		self.assertEqual(frappe.db.get_value("DB Message", name, "delivery_status"), "unknown")

	def test_unconfirmed_reply_kept_when_sync_fails(self):
		from dialogbrain.sync.reconcile import reconcile

		quiet = [_hmsg(1, self.now - timedelta(days=5), "old")]
		conv = self._store("1001", quiet)
		lost = self._reply(conv, "Did this arrive?", 20)
		fake = FakeDialogBrain([_entry("1001")], {"1001": quiet}, fail_history={"1001"})
		self._run(reconcile, fake)
		self.assertEqual(frappe.db.get_value("DB Message", lost, "delivery_status"), "unknown")
		record = self._failures()["1001"]
		self.assertEqual(record["date_from"], _utc_date(self.now - timedelta(minutes=20)))

	# --- tick ---------------------------------------------------------------

	def _tick(self):
		from dialogbrain.sync.reconcile import tick

		with patch("frappe.enqueue") as enqueue:
			tick()
		return [c for c in enqueue.call_args_list if c.args and c.args[0] == "dialogbrain.sync.reconcile.reconcile"]

	def test_tick_interval_public_vs_private(self):
		cases = [
			("public", 120, False),
			("public", 301, True),
			("private", 30, False),
			("private", 61, True),
			# the scheduler ticks every 60 s and the previous pass started a moment after
			# its tick: the next tick sees ~59.x s and must not skip a whole minute (live e2e)
			("private", 58, True),
			("public", 296, True),
		]
		for mode, seconds, expected in cases:
			self._settings(network_mode=mode, last_reconcile_at=now_datetime() - timedelta(seconds=seconds))
			calls = self._tick()
			self.assertEqual(bool(calls), expected, (mode, seconds))
			if calls:
				kwargs = calls[0].kwargs
				self.assertEqual(kwargs["queue"], "long")
				self.assertEqual(kwargs["job_id"], "dialogbrain_reconcile")
				self.assertTrue(kwargs["deduplicate"])

		self._settings(connected=0, last_reconcile_at=now_datetime() - timedelta(days=1))
		with patch("dialogbrain.sync.reconcile.retry_webhook_logs") as retry:
			self.assertEqual(self._tick(), [])
		retry.assert_not_called()

	def test_tick_waits_for_running_backfill(self):
		self._settings(last_reconcile_at=now_datetime() - timedelta(hours=1), backfill_status="running 3/10")
		with patch("dialogbrain.sync.reconcile.is_job_enqueued", return_value=True):
			self.assertEqual(self._tick(), [])
		self.assertEqual(self._get("backfill_status"), "running 3/10")
		with patch("dialogbrain.sync.reconcile.is_job_enqueued", return_value=False):
			self.assertEqual(len(self._tick()), 1)  # the worker died: do not block reconcile forever
		self.assertTrue(self._get("backfill_status").startswith("error: "))

	# --- webhook log maintenance ---------------------------------------------

	def _log(self, status, attempts, received_minutes_ago):
		return (
			frappe.get_doc(
				{
					"doctype": "DB Webhook Log",
					"delivery_id": frappe.generate_hash(length=12),
					"event": "message.received",
					"payload": "{}",
					"received_at": now_datetime() - timedelta(minutes=received_minutes_ago),
					"status": status,
					"attempts": attempts,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def test_retry_failed_and_stuck_logs(self):
		from dialogbrain.sync.reconcile import retry_webhook_logs

		failed = self._log("failed", 1, 3)
		failed_4 = self._log("failed", 4, 30)
		exhausted = self._log("failed", 5, 3)
		stuck = self._log("queued", 0, 11)
		fresh = self._log("queued", 0, 2)
		stuck_exhausted = self._log("queued", 5, 60)
		done = self._log("processed", 1, 60)

		with patch("frappe.enqueue") as enqueue:
			retry_webhook_logs()
		retried = {c.kwargs["log_name"]: c for c in enqueue.call_args_list}
		self.assertEqual(set(retried), {failed, failed_4, stuck})
		for name, call in retried.items():
			self.assertEqual(call.args[0], "dialogbrain.sync.processor.process_delivery")
			self.assertTrue(call.kwargs["deduplicate"])
			self.assertIn(name, call.kwargs["job_id"])
		for name in (exhausted, fresh, stuck_exhausted, done):
			self.assertNotIn(name, retried)

	def test_retry_runs_processor(self):
		"""A retried failed delivery is processed; the 5th failure is the last."""
		from dialogbrain.sync.reconcile import retry_webhook_logs

		name = self._log("failed", 4, 5)
		with patch("frappe.enqueue") as enqueue:
			retry_webhook_logs()
		from dialogbrain.sync.processor import process_delivery

		with patch("dialogbrain.sync.processor.process_event", side_effect=ValueError("still broken")):
			process_delivery(enqueue.call_args.kwargs["log_name"])
		self.assertEqual(frappe.db.get_value("DB Webhook Log", name, "attempts"), 5)
		with patch("frappe.enqueue") as enqueue:
			retry_webhook_logs()
		enqueue.assert_not_called()

	def test_cleanup_webhook_log_14_days(self):
		from dialogbrain.sync.maintenance import cleanup_webhook_log

		old = self._log("processed", 1, 15 * 24 * 60)
		old_failed = self._log("failed", 5, 15 * 24 * 60)
		kept = self._log("processed", 1, 13 * 24 * 60)
		cleanup_webhook_log()
		self.assertFalse(frappe.db.exists("DB Webhook Log", old))
		self.assertFalse(frappe.db.exists("DB Webhook Log", old_failed))
		self.assertTrue(frappe.db.exists("DB Webhook Log", kept))

	def test_scheduler_events(self):
		from dialogbrain import hooks

		self.assertEqual(hooks.scheduler_events["cron"]["* * * * *"], ["dialogbrain.sync.reconcile.tick"])
		self.assertEqual(
			hooks.scheduler_events["daily"],
			[
				"dialogbrain.sync.maintenance.cleanup_webhook_log",
				"dialogbrain.sync.maintenance.cleanup_agent_call_log",
			],
		)
