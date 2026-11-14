from datetime import datetime

import frappe

from dialogbrain.dialogbrain.report.conversations_by_channel_and_manager import (
	conversations_by_channel_and_manager as by_channel,
)
from dialogbrain.dialogbrain.report.first_response_time import first_response_time as frt
from dialogbrain.dialogbrain.report.unanswered_conversations import unanswered_conversations as unanswered
from dialogbrain.tests.test_inbox_api import _channel, _conv, _msg, _user
from dialogbrain.tests.utils import DialogBrainTestCase

CH_A = "rep-a"
CH_B = "rep-b"
REPORTS = ("First Response Time", "Unanswered Conversations", "Conversations by Channel and Manager")
FROM = "2026-03-01"
TO = "2026-03-31"


def _rows(result):
	return result[1]


def _by(rows, key, value):
	return next(r for r in rows if r[key] == value)


class TestReports(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete("DB Draft")
		frappe.db.delete("DB Message Attachment")
		frappe.db.delete("DB Message")
		frappe.db.delete("DB Conversation")
		frappe.db.delete("DB Channel User")
		frappe.db.delete("DB Channel")
		self.mgr_a = _user("rep-mgr-a@example.com", "Chat Manager")
		self.mgr_b = _user("rep-mgr-b@example.com", "Chat Manager")
		self.agent = _user("rep-agent@example.com", "Chat Agent")
		_channel(CH_A, "telegram")
		_channel(CH_B, "whatsapp")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _frt_data(self):
		# manager A: 60 s, 120 s, 600 s and one not answered
		for i, secs in enumerate((60, 120, 600, 0)):
			_conv(CH_A, assigned_to=self.mgr_a, opened_at=f"2026-03-0{i + 1} 10:00:00",
				first_response_seconds=secs)
		# manager B on channel B: 300 s
		_conv(CH_B, assigned_to=self.mgr_b, opened_at="2026-03-01 11:00:00", first_response_seconds=300)
		# unassigned on channel A
		_conv(CH_A, opened_at="2026-03-02 12:00:00", first_response_seconds=30)

	def test_first_response_averages_median_and_share_by_manager(self):
		self._frt_data()
		cols, rows = frt.execute({"from_date": FROM, "to_date": TO, "group_by": "Manager"})
		self.assertEqual(
			[c["fieldname"] for c in cols],
			["group", "conversations", "answered", "not_answered", "avg_min", "median_min",
			 "longest_min", "within_5_pct"],
		)
		a = _by(rows, "group", self.mgr_a)
		self.assertEqual(
			(a["conversations"], a["answered"], a["not_answered"], a["avg_min"], a["median_min"],
			 a["longest_min"], a["within_5_pct"]),
			(4, 3, 1, 4.3, 2.0, 10.0, 66.7),
		)
		b = _by(rows, "group", self.mgr_b)
		self.assertEqual((b["conversations"], b["answered"], b["avg_min"], b["within_5_pct"]),
			(1, 1, 5.0, 100.0))
		u = _by(rows, "group", "Unassigned")
		self.assertEqual((u["conversations"], u["avg_min"], u["median_min"]), (1, 0.5, 0.5))

	def test_first_response_by_channel_and_by_day(self):
		self._frt_data()
		_, rows = frt.execute({"from_date": FROM, "to_date": TO, "group_by": "Channel"})
		a = _by(rows, "group", CH_A)
		self.assertEqual((a["conversations"], a["answered"], a["not_answered"]), (5, 4, 1))
		self.assertEqual(_by(rows, "group", CH_B)["conversations"], 1)
		_, rows = frt.execute({"from_date": FROM, "to_date": TO, "group_by": "Day"})
		self.assertEqual([str(r["group"]) for r in rows], ["2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04"])
		d1 = _by(rows, "group", datetime(2026, 3, 1).date()) if not isinstance(rows[0]["group"], str) \
			else _by(rows, "group", "2026-03-01")
		self.assertEqual((d1["conversations"], d1["answered"]), (2, 2))
		self.assertEqual(d1["median_min"], 3.0)  # 60 s and 300 s -> 180 s

	def test_first_response_respects_the_period_and_filters(self):
		self._frt_data()
		_conv(CH_A, assigned_to=self.mgr_a, opened_at="2026-02-28 23:59:00", first_response_seconds=60)
		_conv(CH_A, assigned_to=self.mgr_a, opened_at="2026-04-01 00:00:00", first_response_seconds=60)
		_, rows = frt.execute({"from_date": FROM, "to_date": TO, "group_by": "Manager"})
		self.assertEqual(_by(rows, "group", self.mgr_a)["conversations"], 4)  # edges excluded
		_, rows = frt.execute({"from_date": FROM, "to_date": TO, "group_by": "Manager", "channel": CH_B})
		self.assertEqual([r["group"] for r in rows], [self.mgr_b])
		_, rows = frt.execute({"from_date": FROM, "to_date": TO, "group_by": "Channel",
			"assigned_to": self.mgr_a})
		self.assertEqual([(r["group"], r["conversations"]) for r in rows], [(CH_A, 4)])

	def test_unanswered_lists_open_chats_waiting_for_us_oldest_first(self):
		old = _conv(CH_A, title="old", last_message_direction="in", last_inbound_at="2020-01-01 10:00:00",
			unread_count=3, last_message_preview="hello")
		new = _conv(CH_A, title="new", last_message_direction="in", last_inbound_at="2020-01-02 10:00:00")
		_conv(CH_A, title="closed", status="Closed", last_message_direction="in",
			last_inbound_at="2020-01-01 09:00:00")
		_conv(CH_A, title="answered", last_message_direction="out", last_inbound_at="2020-01-01 08:00:00")
		cols, rows = unanswered.execute({})
		self.assertEqual([r["conversation"] for r in rows], [old, new])
		self.assertEqual(rows[0]["title"], "old")
		self.assertEqual(rows[0]["unread_count"], 3)
		self.assertEqual(rows[0]["last_message_preview"], "hello")
		self.assertGreater(rows[0]["waiting_min"], 60 * 24 * 365)
		self.assertIn("waiting_min", [c["fieldname"] for c in cols])
		_, rows = unanswered.execute({"channel": CH_B})
		self.assertEqual(rows, [])

	def test_unanswered_min_waiting_minutes(self):
		from frappe.utils import add_to_date, now_datetime

		long_wait = _conv(CH_A, last_message_direction="in",
			last_inbound_at=add_to_date(now_datetime(), minutes=-120))
		_conv(CH_A, last_message_direction="in", last_inbound_at=add_to_date(now_datetime(), minutes=-5))
		_, rows = unanswered.execute({"min_waiting_minutes": 60})
		self.assertEqual([r["conversation"] for r in rows], [long_wait])
		_, rows = unanswered.execute({"min_waiting_minutes": 0})
		self.assertEqual(len(rows), 2)
		_, rows = unanswered.execute({"assigned_to": self.mgr_a})
		self.assertEqual(rows, [])

	def test_by_channel_counts_opened_closed_and_messages(self):
		a1 = _conv(CH_A, assigned_to=self.mgr_a, opened_at="2026-03-05 10:00:00",
			last_message_direction="in")
		a2 = _conv(CH_A, assigned_to=self.mgr_a, opened_at="2026-03-06 10:00:00", status="Closed",
			closed_at="2026-03-07 10:00:00", last_message_direction="out")
		b1 = _conv(CH_B, assigned_to=self.mgr_b, opened_at="2026-02-01 10:00:00",
			closed_at="2026-03-10 10:00:00", status="Closed")
		_msg(a1, direction="in", sent_at="2026-03-05 10:00:00")
		_msg(a1, direction="in", sent_at="2026-03-05 10:01:00")
		_msg(a1, direction="out", sent_at="2026-03-05 10:02:00")
		_msg(a1, direction="out", sent_at="2026-03-05 10:03:00", author_type="agent")
		_msg(a2, direction="out", sent_at="2026-03-06 10:03:00", author_type="agent")
		_msg(a2, direction="in", sent_at="2026-04-06 10:03:00")  # outside the period
		_msg(b1, direction="in", sent_at="2026-03-11 10:00:00")
		cols, rows = by_channel.execute({"from_date": FROM, "to_date": TO, "group_by": "Channel"})
		self.assertEqual(
			[c["fieldname"] for c in cols],
			["group", "opened", "closed", "open_now", "unanswered_now", "inbound", "outbound", "by_ai"],
		)
		a = _by(rows, "group", CH_A)
		self.assertEqual(
			(a["opened"], a["closed"], a["open_now"], a["unanswered_now"], a["inbound"], a["outbound"], a["by_ai"]),
			(2, 1, 1, 1, 2, 3, 2),
		)
		b = _by(rows, "group", CH_B)
		self.assertEqual((b["opened"], b["closed"], b["open_now"], b["inbound"], b["outbound"]),
			(0, 1, 0, 1, 0))
		_, rows = by_channel.execute({"from_date": FROM, "to_date": TO, "group_by": "Manager"})
		ma = _by(rows, "group", self.mgr_a)
		self.assertEqual((ma["opened"], ma["closed"], ma["inbound"], ma["outbound"], ma["by_ai"]),
			(2, 1, 2, 3, 2))

	def test_empty_period_returns_no_rows_not_an_error(self):
		args = {"from_date": "2001-01-01", "to_date": "2001-01-31"}
		self.assertEqual(_rows(frt.execute(dict(args, group_by="Manager"))), [])
		self.assertEqual(_rows(by_channel.execute(dict(args, group_by="Channel"))), [])
		self.assertEqual(_rows(unanswered.execute({})), [])

	def test_chat_agent_can_not_run_the_reports(self):
		frappe.set_user(self.agent)
		for name in REPORTS:
			self.assertFalse(frappe.get_doc("Report", name).is_permitted(), name)
		for mod in (frt, unanswered, by_channel):
			with self.assertRaises(frappe.PermissionError):
				mod.execute({})
		frappe.set_user(self.mgr_a)
		for name in REPORTS:
			self.assertTrue(frappe.get_doc("Report", name).is_permitted(), name)

	def test_report_sql_takes_values_as_parameters(self):
		self._frt_data()
		evil = "x' OR '1'='1"
		self.assertEqual(_rows(frt.execute({"from_date": FROM, "to_date": TO, "channel": evil})), [])
		self.assertEqual(_rows(unanswered.execute({"channel": evil, "assigned_to": evil})), [])
		self.assertEqual(
			_rows(frt.execute({"from_date": FROM, "to_date": TO, "assigned_to": evil, "group_by": "Channel"})), [])
