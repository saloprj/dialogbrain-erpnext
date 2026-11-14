"""The drain: one background job per lane that processes every queued delivery in order."""

import json
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from dialogbrain.tests.utils import DialogBrainTestCase


def _log(lane, minutes_ago, status="queued", attempts=0, marker=None):
	marker = marker or frappe.generate_hash(length=8)
	doc = {
		"doctype": "DB Webhook Log",
		"delivery_id": f"drain-{marker}",
		"event": "message.received",
		"payload": json.dumps({"event": "message.received", "data": {"marker": marker}}),
		"received_at": now_datetime() - timedelta(minutes=minutes_ago),
		"status": status,
		"attempts": attempts,
	}
	if lane is not None:
		doc["lane"] = lane
	return frappe.get_doc(doc).insert(ignore_permissions=True).name


class TestDrain(DialogBrainTestCase):
	def setUp(self):
		frappe.db.delete("DB Webhook Log", {"delivery_id": ["like", "drain-%"]})

	def _run(self, lane="short", **kwargs):
		from dialogbrain.sync.processor import drain

		seen = []
		with patch("dialogbrain.sync.processor.process_event", side_effect=lambda e, d: seen.append(d["marker"])):
			drain(lane, **kwargs)
		return seen

	def _status(self, name):
		return frappe.db.get_value("DB Webhook Log", name, "status")

	def test_drain_processes_its_lane_oldest_first_and_leaves_the_other_lane(self):
		c = _log("short", 1, marker="c")
		a = _log("short", 3, marker="a")
		b = _log("short", 2, marker="b")
		other = _log("long", 5, marker="long")
		done = _log("short", 9, status="processed", marker="done")
		self.assertEqual(self._run("short"), ["a", "b", "c"])
		self.assertEqual([self._status(n) for n in (a, b, c)], ["processed"] * 3)
		self.assertEqual(self._status(other), "queued")
		self.assertEqual(self._status(done), "processed")
		self.assertEqual(self._run("long"), ["long"])

	def test_drain_works_in_batches_until_the_lane_is_empty(self):
		for i in range(5):
			_log("short", 10 - i, marker=f"m{i}")
		self.assertEqual(self._run("short", batch_size=2), ["m0", "m1", "m2", "m3", "m4"])

	def test_drain_commits_after_every_delivery(self):
		"""A killed drain keeps what it finished; a row never waits for the whole batch."""
		for i in range(3):
			_log("short", 5 - i, marker=f"m{i}")
		with patch("dialogbrain.sync.processor._commit") as commit, patch(
			"dialogbrain.sync.processor.process_event"
		):
			from dialogbrain.sync.processor import drain

			drain("short")
		# process_delivery commits the attempt once per row; the drain commits the result once per row
		self.assertGreaterEqual(commit.call_count, 6)

	def test_drain_does_not_loop_on_rows_past_the_attempt_cap(self):
		stuck = _log("short", 20, attempts=5, marker="stuck")
		fresh = _log("short", 1, marker="fresh")
		self.assertEqual(self._run("short"), ["fresh"])
		self.assertEqual(self._status(stuck), "queued")

	def test_drain_takes_rows_without_a_lane_as_short(self):
		legacy = _log(None, 3, marker="legacy")
		self.assertEqual(self._run("short"), ["legacy"])
		self.assertEqual(self._status(legacy), "processed")

	def test_drain_keeps_going_after_a_failed_delivery(self):
		bad = _log("short", 3, marker="bad")
		good = _log("short", 1, marker="good")
		from dialogbrain.sync.processor import drain

		def handler(event, data):
			if data["marker"] == "bad":
				raise ValueError("boom")

		with patch("dialogbrain.sync.processor.process_event", side_effect=handler):
			drain("short")
		self.assertEqual((self._status(bad), self._status(good)), ("failed", "processed"))

	def test_drain_survives_a_delivery_that_raises(self):
		"""process_delivery's own guard does not cover its FOR UPDATE reads (a lock wait
		escapes); one such row must not kill the whole lane's job."""
		bad = _log("short", 3, marker="bad")
		good = _log("short", 1, marker="good")
		from dialogbrain.sync import processor

		real = processor.process_delivery

		def flaky(name):
			if name == bad:
				raise frappe.QueryTimeoutError("lock wait")
			return real(name)

		with patch.object(processor, "process_delivery", side_effect=flaky), patch(
			"dialogbrain.sync.processor.process_event"
		), patch("frappe.log_error") as log_error:
			processor.drain("short", max_batches=1)
		self.assertEqual((self._status(bad), self._status(good)), ("queued", "processed"))
		log_error.assert_called_once()

	def test_drain_stops_after_max_batches(self):
		"""A bounded job: the RQ timeout never kills it mid-row; the next kick continues."""
		for i in range(4):
			_log("short", 10 - i, marker=f"m{i}")
		self.assertEqual(self._run("short", batch_size=1, max_batches=2), ["m0", "m1"])
