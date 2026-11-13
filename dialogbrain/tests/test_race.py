"""A conversation committed by another transaction after this one's snapshot.

MariaDB (and Frappe's Postgres connection) run at REPEATABLE READ: once a transaction has
read, rows other transactions commit later stay invisible to its plain reads. Live, the
webhook processor handles ``message.received`` and ``conversation.started`` of a new chat
concurrently: the loser's insert hits the winner's unique key, and every plain read after
that (the re-lookup, Frappe's link validation of DB Message.conversation) misses the row.

These tests use a second database connection and real commits, so they clean up after
themselves explicitly (all ids are unique to this module).
"""

import copy
import json
from unittest.mock import patch

import frappe

from dialogbrain.tests.utils import DialogBrainTestCase, load_fixture

CHANNEL = "990155"
THREAD_ID = "990001"
THREAD_REF = "telegram:990000001"
DB_ID = "990000001"


def _payload(with_internal_id=True):
	body = copy.deepcopy(load_fixture("webhook_message_received_inbound"))
	body["delivery_id"] = "race-990001"
	data = body["data"]
	data["account_id"] = CHANNEL
	data["thread_id"] = THREAD_REF
	data["thread"]["db_id"] = THREAD_ID
	data["thread"]["thread_id"] = THREAD_REF
	data["message"]["db_id"] = DB_ID
	data["message"]["message_id"] = "telegram:990000001"
	if not with_internal_id:  # e.g. the messages.send echo: thread known by its ref only
		data["thread"].pop("db_id", None)
	return body


def _second_connection():
	from frappe.database import get_db

	conf = frappe.local.conf
	return get_db(
		host=conf.db_host,
		port=conf.db_port,
		user=conf.db_user or conf.db_name,
		password=conf.db_password,
		cur_db_name=conf.db_name,
	)


def _cleanup():
	conversations = frappe.db.sql_list(
		"select name from `tabDB Conversation` where thread_id = %s or (channel = %s and thread_ref = %s)",
		(THREAD_ID, CHANNEL, THREAD_REF),
	)
	if conversations:
		frappe.db.delete("DB Message", {"conversation": ["in", conversations]})
		frappe.db.delete("Notification Log", {"document_name": ["in", conversations]})
		frappe.db.delete("DB Conversation", {"name": ["in", conversations]})
	frappe.db.delete("DB Message", {"db_id": DB_ID})
	frappe.db.delete("DB Webhook Log", {"delivery_id": "race-990001"})
	frappe.db.delete("DB Channel", {"name": CHANNEL})
	frappe.db.commit()


class TestConversationRace(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_cleanup()

	def tearDown(self):
		frappe.db.rollback()
		_cleanup()

	def _winner_commits_conversation(self):
		"""Another worker inserts and commits the conversation (raw SQL on its own connection)."""
		db2 = _second_connection()
		try:
			db2.sql(
				"""insert into `tabDB Conversation`
				(name, creation, modified, owner, modified_by, docstatus, idx,
				 thread_id, thread_ref, channel, status, unread_count, first_response_seconds)
				values ('race-winner', now(), now(), 'Administrator', 'Administrator', 0, 0,
				 %s, %s, %s, 'Open', 0, 0)""",
				(THREAD_ID, THREAD_REF, CHANNEL),
			)
			db2.commit()
		finally:
			db2.close()

	def test_message_received_after_losing_the_conversation_race(self):
		self._race(with_internal_id=True)

	def test_ref_only_message_after_losing_the_conversation_race(self):
		self._race(with_internal_id=False)

	def _race(self, with_internal_id):
		from dialogbrain.sync import processor

		frappe.get_doc(
			{"doctype": "DB Channel", "account_id": CHANNEL, "channel_type": "telegram", "title": "Race"}
		).insert(ignore_permissions=True)
		body = _payload(with_internal_id)
		log = (
			frappe.get_doc(
				{
					"doctype": "DB Webhook Log",
					"delivery_id": body["delivery_id"],
					"event": body["event"],
					"payload": json.dumps(body),
					"status": "queued",
					"attempts": 0,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

		real_process_event = processor.process_event
		raced = []

		def process_event(event, data):
			if not raced:
				raced.append(True)
				# this transaction's snapshot starts here (a plain read) ...
				self.assertFalse(frappe.db.get_value("DB Conversation", {"thread_id": THREAD_ID}))
				# ... and only then the other worker commits the conversation
				self._winner_commits_conversation()
			return real_process_event(event, data)

		with patch.object(processor, "_commit", side_effect=frappe.db.commit), patch.object(
			processor, "process_event", side_effect=process_event
		):
			processor.process_delivery(log_name=log)

		row = frappe.db.get_value("DB Webhook Log", log, ["status", "error"], as_dict=True)
		self.assertEqual(row.status, "processed", row.error)
		frappe.db.commit()
		message = frappe.db.get_value("DB Message", {"db_id": DB_ID}, ["conversation", "direction"], as_dict=True)
		self.assertEqual(message.conversation, "race-winner")
		self.assertEqual(message.direction, "in")
		self.assertEqual(frappe.db.count("DB Conversation", {"thread_id": THREAD_ID}), 1)
