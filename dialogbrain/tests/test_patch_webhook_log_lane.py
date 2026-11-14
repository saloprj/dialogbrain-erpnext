"""Patch: rows queued before the ``lane`` column existed keep their routing."""

import json

import frappe

from dialogbrain.tests.utils import DialogBrainTestCase


def _row(skip_push, status="queued"):
	return (
		frappe.get_doc(
			{
				"doctype": "DB Webhook Log",
				"delivery_id": "lanepatch-" + frappe.generate_hash(length=8),
				"event": "message.received",
				"payload": json.dumps({"event": "message.received", "data": {"skip_push": skip_push}}),
				"status": status,
				"attempts": 0,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


class TestWebhookLogLanePatch(DialogBrainTestCase):
	def test_queued_backfill_rows_go_to_long_and_the_rest_to_short(self):
		from dialogbrain.patches.v0_1.set_webhook_log_lane import execute

		backfill = _row(True)
		live = _row(False)
		done = _row(True, status="processed")
		for name in (backfill, live, done):
			frappe.db.set_value("DB Webhook Log", name, "lane", None, update_modified=False)
		execute()
		lanes = {n: frappe.db.get_value("DB Webhook Log", n, "lane") for n in (backfill, live, done)}
		self.assertEqual(lanes, {backfill: "long", live: "short", done: "short"})
