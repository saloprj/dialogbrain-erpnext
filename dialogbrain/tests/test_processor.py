import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import frappe
from frappe.utils import get_datetime

from dialogbrain.permissions import permitted_users
from dialogbrain.sync.upsert import to_system_datetime
from dialogbrain.tests.utils import DialogBrainTestCase, load_fixture

INBOUND = "webhook_message_received_inbound"
GROUP = "webhook_message_received_group"
OUTGOING = "webhook_message_received_outgoing"
OUTGOING_BY_REF = "webhook_message_received_outgoing_by_ref"
DELETED = "webhook_message_deleted"
UPDATED = "webhook_message_updated_derived"


def _user(email, *roles):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	user = frappe.get_doc(
		{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
	)
	user.insert(ignore_permissions=True)
	user.add_roles(*roles)
	return email


def _iso(dt):
	return dt.astimezone(timezone.utc).isoformat()


def _data(fixture, **message_overrides):
	"""The ``data`` of a recorded webhook with ``message`` fields overridden."""
	data = copy.deepcopy(load_fixture(fixture)["data"])
	data["message"].update(message_overrides)
	return data


def _process(fixture, **message_overrides):
	from dialogbrain.sync.processor import process_event

	event = load_fixture(fixture)["event"]
	data = _data(fixture, **message_overrides) if message_overrides else copy.deepcopy(load_fixture(fixture)["data"])
	process_event(event, data)
	return data


def _conv_by_thread(thread_id):
	return frappe.db.get_value("DB Conversation", {"thread_id": thread_id})


def _msg(db_id):
	name = frappe.db.get_value("DB Message", {"db_id": db_id})
	return frappe.get_doc("DB Message", name) if name else None


def _logs(conversation):
	return frappe.get_all(
		"Notification Log",
		filters={"document_type": "DB Conversation", "document_name": conversation},
		pluck="for_user",
	)


class TestProcessor(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for dt in (
			"DB Draft",
			"DB Message Attachment",
			"DB Message",
			"DB Conversation",
			"DB Channel User",
			"DB Channel",
			"DB Webhook Log",
		):
			frappe.db.delete(dt)
		frappe.db.delete("Notification Log", {"document_type": "DB Conversation"})
		# Other test modules leave users with chat roles behind: neutralise them so
		# recipient lists are exactly ours.
		frappe.db.sql(
			"""DELETE FROM `tabHas Role` WHERE role IN ('Chat Manager', 'Chat Agent')
			AND parent NOT LIKE 'proc-%%'"""
		)
		self.manager = _user("proc-manager@example.com", "Chat Manager")
		self.agent = _user("proc-agent@example.com", "Chat Agent")
		self.other = _user("proc-other@example.com", "Chat Agent")

	# --- creation & content -------------------------------------------------

	def test_message_received_creates_conversation_and_channel(self):
		self.assertFalse(frappe.db.exists("DB Channel", "155"))
		_process(INBOUND)
		channel = frappe.get_doc("DB Channel", "155")
		self.assertEqual(channel.channel_type, "telegram")
		conv = frappe.get_doc("DB Conversation", _conv_by_thread("1001"))
		self.assertEqual(conv.thread_ref, "telegram:100000001")
		self.assertEqual(conv.channel, "155")
		self.assertEqual(conv.title, "Alice Example")
		self.assertEqual(conv.contact_name, "Alice Example")
		self.assertEqual(conv.handle, "alice_example")
		self.assertEqual(conv.phone, "10000000001")
		self.assertEqual(conv.status, "Open")
		self.assertEqual(conv.unread_count, 1)
		self.assertEqual(conv.last_message_direction, "in")
		msg = _msg("9000001")
		self.assertEqual(msg.conversation, conv.name)
		self.assertEqual(msg.message_id, "telegram:5000001")
		self.assertEqual(msg.direction, "in")
		self.assertEqual(msg.author_type, "customer")
		self.assertEqual(msg.text, "Hi, please send me the offer for the project, thanks.")
		self.assertIsNone(msg.client_ref)

	def test_group_message_uses_thread_title(self):
		_process(GROUP)
		conv = frappe.get_doc("DB Conversation", _conv_by_thread("1002"))
		self.assertEqual(conv.title, "Example Group")
		self.assertEqual(conv.thread_ref, "telegram:-1000000000002")
		self.assertFalse(conv.phone)
		self.assertEqual(_msg("9000002").author_type, "customer")

	def test_long_thread_title_and_contact_fields_fit(self):
		"""Seen live: an email-group title of 200+ characters failed the backfill."""
		from dialogbrain.sync.processor import process_event

		data = copy.deepcopy(load_fixture(GROUP)["data"])
		data["thread"]["title"] = "Re: Invitation " + "x" * 300
		process_event("message.received", data)
		conv = frappe.get_doc("DB Conversation", _conv_by_thread("1002"))
		self.assertEqual(len(conv.title), 140)
		self.assertTrue(conv.title.startswith("Re: Invitation xx"))

		from dialogbrain.sync.upsert import upsert_conversation

		upsert_conversation(
			{"thread_id": "1002", "title": "y" * 500, "avatar_url": "https://a.example/" + "z" * 300},
			conv.channel,
		)
		conv.reload()
		self.assertEqual(len(conv.title), 140)
		self.assertFalse(conv.avatar_url)  # a cut URL is useless: dropped

	def test_conversation_race_relooks_up_with_locking_read(self):
		"""Seen live: ``message.received`` and ``conversation.started`` for a new chat are
		processed concurrently; the loser's duplicate insert must find the winner's row.
		A plain read in the loser's (REPEATABLE READ) snapshot cannot see it, so the
		re-lookup is a locking read, which reads the latest committed row."""
		from dialogbrain.sync import upsert

		calls = []

		def find(internal=None, ref=None, channel=None, for_update=False):
			calls.append(for_update)
			return "WINNER" if for_update else None

		with patch.object(upsert, "find_conversation", side_effect=find), patch.object(
			upsert, "_insert", return_value=False
		), patch.object(frappe.db, "get_value", return_value="8455139"):
			name, created = upsert.upsert_conversation({"thread_id": "8455139"}, "201")
		self.assertEqual((name, created), ("WINNER", False))
		self.assertEqual(calls, [False, True])

	def test_echo_thread_without_db_id_uses_internal_id(self):
		# messages.send echoes carry thread.thread_id "livechat:<internal id>", no db_id
		_process(OUTGOING)
		_process(OUTGOING_BY_REF)
		convs = frappe.get_all("DB Conversation", fields=["thread_id", "thread_ref"])
		self.assertEqual(len(convs), 1)
		self.assertEqual(convs[0].thread_id, "8451249")
		self.assertEqual(
			convs[0].thread_ref, "livechat:a07f9d4a-7e2c-4626-b785-13ba088dd210:sim_c91803a9acfe"
		)

	def test_ref_only_then_id_event_fills_thread_id(self):
		_process(UPDATED)  # carries only the channel ref
		conv = frappe.get_doc("DB Conversation", frappe.db.get_value("DB Conversation", {"thread_ref": "telegram:100000001"}))
		self.assertIsNone(conv.thread_id)
		_process(INBOUND, db_id="9000003", message_id="telegram:5000003")
		self.assertEqual(frappe.db.count("DB Conversation"), 1)
		self.assertEqual(frappe.db.get_value("DB Conversation", conv.name, "thread_id"), "1001")

	def test_same_ref_on_two_accounts_is_two_conversations(self):
		from dialogbrain.sync.processor import process_event

		for account in ("155", "156"):  # ref-only events, no internal id
			data = copy.deepcopy(load_fixture(UPDATED)["data"])
			data["account_id"] = account
			data["message"]["db_id"] = "db-" + account
			process_event("message.updated", data)
			process_event("message.updated", data)  # retry: no DuplicateEntryError
		rows = frappe.get_all("DB Conversation", fields=["channel", "thread_ref", "thread_id"], order_by="channel")
		self.assertEqual([(r.channel, r.thread_ref, r.thread_id) for r in rows], [
			("155", "telegram:100000001", None),
			("156", "telegram:100000001", None),
		])

	def test_concurrent_conversation_insert_relooks_up(self):
		"""Another worker created the (channel, thread_ref) row between lookup and insert."""
		from dialogbrain.sync import upsert

		_process(UPDATED)
		existing = frappe.db.get_value("DB Conversation", {"thread_ref": "telegram:100000001"})
		real = upsert.find_conversation
		calls = []

		def misses_once(*args, **kwargs):
			calls.append(args)
			return None if len(calls) == 1 else real(*args, **kwargs)

		with patch.object(upsert, "find_conversation", side_effect=misses_once):
			name, created = upsert.upsert_conversation(
				{"db_id": "1001", "channel_ref": "telegram:100000001"}, "155"
			)
		self.assertEqual((name, created), (existing, False))
		self.assertEqual(frappe.db.count("DB Conversation"), 1)
		self.assertEqual(frappe.db.get_value("DB Conversation", existing, "thread_id"), "1001")

	def test_duplicate_delivery_with_attachments_does_not_republish(self):
		att = [{"file_id": 5, "filename": "a.png", "mime_type": "image/png", "media_type": "photo"}]
		_process(INBOUND, attachments=att)
		with patch("frappe.publish_realtime") as pub:
			_process(INBOUND, attachments=att)
		self.assertFalse(pub.call_args_list)

	def test_long_message_stored_in_full(self):
		body = "x" * 300
		_process(INBOUND, html=body, text=body[:100])
		self.assertEqual(_msg("9000001").text, body)
		# the recorded echo: text 100 chars, html 184
		_process(OUTGOING)
		msg = _msg("27771418")
		self.assertEqual(len(msg.text), 184)

	def test_attachments_stored_without_urls(self):
		_process(
			INBOUND,
			attachments=[
				{
					"file_id": 77,
					"filename": "offer.pdf",
					"mime_type": "application/pdf",
					"download_url": "https://files.example/secret-token",
					"media_type": "document",
				}
			],
		)
		msg = _msg("9000001")
		self.assertEqual(len(msg.attachments), 1)
		self.assertEqual(msg.attachments[0].file_id, "77")
		self.assertEqual(msg.attachments[0].filename, "offer.pdf")
		self.assertNotIn("secret-token", json.dumps(msg.raw if isinstance(msg.raw, dict) else msg.raw or ""))

	# --- authorship ---------------------------------------------------------

	def test_direction_and_author_agent(self):
		_process(OUTGOING, from_name="Sales Bot", sender={"id": "system", "name": "Sales Bot"})
		msg = _msg("27771418")
		self.assertEqual(msg.direction, "out")
		self.assertEqual(msg.author_type, "agent")
		self.assertFalse(msg.sent_by)

	def test_out_from_dialogbrain_ui_is_manager_without_sent_by(self):
		_process(OUTGOING)
		msg = _msg("27771418")
		self.assertEqual(msg.direction, "out")
		self.assertEqual(msg.author_type, "manager")
		self.assertFalse(msg.sent_by)

	def test_echo_matches_pending_reply(self):
		from dialogbrain.sync.upsert import upsert_channel, upsert_conversation

		data = _data(OUTGOING)
		channel = upsert_channel("201", "livechat")
		conv, _ = upsert_conversation({"id": "8451249", "channel_ref": data["thread_id"]}, channel)
		now = datetime.now(timezone.utc)
		pending = frappe.get_doc(
			{
				"doctype": "DB Message",
				"conversation": conv,
				"direction": "out",
				"author_type": "manager",
				"sent_by": self.agent,
				"text": data["message"]["html"],
				"delivery_status": "pending",
				"client_ref": "cref-1",
				"sent_at": frappe.utils.now_datetime(),
			}
		).insert(ignore_permissions=True)
		_process(OUTGOING, sent_at=_iso(now + timedelta(seconds=5)))
		rows = frappe.get_all("DB Message", filters={"conversation": conv}, fields=["name", "db_id", "sent_by", "author_type"])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].name, pending.name)
		self.assertEqual(rows[0].db_id, "27771418")
		self.assertEqual(rows[0].sent_by, self.agent)
		self.assertEqual(rows[0].author_type, "manager")

	# --- idempotency --------------------------------------------------------

	def test_duplicate_db_id_is_noop(self):
		_process(INBOUND)
		_process(INBOUND)
		self.assertEqual(frappe.db.count("DB Message", {"db_id": "9000001"}), 1)
		conv = frappe.get_doc("DB Conversation", _conv_by_thread("1001"))
		self.assertEqual(conv.unread_count, 1)

	def test_duplicate_race_swallowed(self):
		"""Another worker inserted the same db_id between our lookup and our insert."""
		from frappe.model.document import Document

		from dialogbrain.sync import upsert

		_process(INBOUND)
		conv = _conv_by_thread("1001")
		message = _data(INBOUND)["message"]
		existing = frappe.db.get_value("DB Message", {"db_id": "9000001"})
		real_lookup = upsert._message_by_db_id
		real_insert = Document.insert
		calls = {"lookup": 0, "insert": 0}

		def lookup_misses_once(db_id):
			calls["lookup"] += 1
			return None if calls["lookup"] == 1 else real_lookup(db_id)

		def insert_raises_once(doc, *args, **kwargs):
			if doc.doctype == "DB Message" and not calls["insert"]:
				calls["insert"] += 1
				raise frappe.DuplicateEntryError("DB Message", "9000001")
			return real_insert(doc, *args, **kwargs)

		with patch.object(upsert, "_message_by_db_id", side_effect=lookup_misses_once), patch.object(
			Document, "insert", autospec=True, side_effect=insert_raises_once
		):
			name, created = upsert.upsert_message(conv, message)
		self.assertEqual((name, created), (existing, False))
		self.assertEqual(frappe.db.count("DB Message", {"db_id": "9000001"}), 1)

	def test_duplicate_race_real_unique_index(self):
		from dialogbrain.sync import upsert

		_process(INBOUND)
		conv = _conv_by_thread("1001")
		existing = frappe.db.get_value("DB Message", {"db_id": "9000001"})
		with patch.object(upsert, "_message_by_db_id", side_effect=[None, existing]):
			name, created = upsert.upsert_message(conv, _data(INBOUND)["message"])
		self.assertEqual((name, created), (existing, False))
		self.assertEqual(frappe.db.count("DB Message", {"db_id": "9000001"}), 1)

	def test_updated_event_for_unknown_message_upserts(self):
		_process(UPDATED)
		msg = _msg("9000001")
		self.assertIsNotNone(msg)
		self.assertEqual(msg.message_id, "telegram:5000001")
		# provisional: direction unknown -> no direction/author, counters or alerts
		self.assertFalse(msg.direction)
		self.assertFalse(msg.author_type)
		conv = frappe.get_doc("DB Conversation", msg.conversation)
		self.assertEqual(conv.thread_ref, "telegram:100000001")
		self.assertEqual(conv.channel, "155")
		self.assertFalse(conv.unread_count)
		self.assertFalse(conv.last_message_at)
		self.assertEqual(_logs(conv.name), [])

	def test_partial_then_full_inbound_counts_once(self):
		_process(UPDATED)
		with patch("frappe.publish_realtime") as pub:
			_process(INBOUND)
		msg = _msg("9000001")
		self.assertEqual((msg.direction, msg.author_type), ("in", "customer"))
		self.assertEqual(msg.text, "Hi, please send me the offer for the project, thanks.")
		conv = frappe.get_doc("DB Conversation", msg.conversation)
		self.assertEqual(conv.unread_count, 1)
		self.assertEqual(conv.last_message_preview, msg.text)
		self.assertEqual(get_datetime(conv.last_inbound_at), to_system_datetime("2026-11-13T12:06:24+00:00"))
		self.assertEqual(sorted(_logs(conv.name)), sorted([self.manager, self.agent, self.other]))
		self.assertTrue([c for c in pub.call_args_list if c.args and c.args[0] == "dialogbrain:message"])
		_process(INBOUND)  # redelivery: no second count
		self.assertEqual(frappe.db.get_value("DB Conversation", conv.name, "unread_count"), 1)
		self.assertEqual(len(_logs(conv.name)), 3)

	def test_partial_then_full_outgoing(self):
		_process(UPDATED)
		data = _data(
			INBOUND,
			is_outgoing=True,
			from_id="system",
			from_name="Sales Bot",
			sender={"id": "system", "name": "Sales Bot"},
			html="Our offer is attached",
		)
		from dialogbrain.sync.processor import process_event

		process_event("message.received", data)
		msg = _msg("9000001")
		self.assertEqual((msg.direction, msg.author_type, msg.delivery_status), ("out", "agent", "sent"))
		conv = frappe.get_doc("DB Conversation", msg.conversation)
		self.assertFalse(conv.unread_count)
		self.assertEqual(conv.last_message_direction, "out")
		self.assertEqual(conv.last_message_preview, "Our offer is attached")
		self.assertEqual(_logs(conv.name), [])
		self.assertEqual(frappe.db.count("DB Message"), 1)

	def test_partial_then_echo_folds_into_pending_reply(self):
		from dialogbrain.sync.processor import process_event
		from dialogbrain.sync.upsert import upsert_channel, upsert_conversation

		data = _data(OUTGOING)
		conv, _ = upsert_conversation({"id": "8451249", "channel_ref": data["thread_id"]}, upsert_channel("201", "livechat"))
		pending = frappe.get_doc(
			{
				"doctype": "DB Message",
				"conversation": conv,
				"direction": "out",
				"author_type": "manager",
				"sent_by": self.agent,
				"text": data["message"]["html"],
				"delivery_status": "pending",
				"sent_at": frappe.utils.now_datetime(),
			}
		).insert(ignore_permissions=True)
		partial = {k: data[k] for k in ("channel", "account_id", "thread_id")}
		partial["message"] = {"db_id": "27771418", "message_id": data["message"]["message_id"], "meta": {}}
		process_event("message.updated", partial)
		_process(OUTGOING, sent_at=_iso(datetime.now(timezone.utc)))
		rows = frappe.get_all("DB Message", fields=["name", "db_id", "sent_by"])
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0].name, rows[0].db_id, rows[0].sent_by), (pending.name, "27771418", self.agent))

	def test_updated_event_without_html_keeps_text(self):
		_process(INBOUND)
		_process(UPDATED)
		self.assertEqual(_msg("9000001").text, "Hi, please send me the offer for the project, thanks.")
		self.assertEqual(frappe.db.count("DB Message"), 1)
		self.assertEqual(frappe.db.count("DB Conversation"), 1)

	def test_updated_event_with_html_replaces_text(self):
		_process(INBOUND)
		from dialogbrain.sync.processor import process_event

		data = _data(INBOUND, html="Edited body", text="Edited body")
		process_event("message.updated", data)
		self.assertEqual(_msg("9000001").text, "Edited body")
		self.assertEqual(frappe.get_doc("DB Conversation", _conv_by_thread("1001")).unread_count, 1)

	# --- counters -----------------------------------------------------------

	def test_out_of_order_keeps_latest_preview(self):
		t = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
		_process(INBOUND, db_id="2", message_id="telegram:2", html="later", sent_at=_iso(t + timedelta(minutes=1)))
		_process(INBOUND, db_id="1", message_id="telegram:1", html="earlier", sent_at=_iso(t))
		conv = frappe.get_doc("DB Conversation", _conv_by_thread("1001"))
		self.assertEqual(conv.last_message_preview, "later")
		self.assertEqual(get_datetime(conv.last_message_at), to_system_datetime(_iso(t + timedelta(minutes=1))))
		self.assertEqual(conv.unread_count, 2)

	def test_inbound_reopens_closed_conversation(self):
		t = datetime.now(timezone.utc)
		_process(INBOUND, db_id="1", sent_at=_iso(t - timedelta(minutes=5)))
		name = _conv_by_thread("1001")
		frappe.db.set_value(
			"DB Conversation",
			name,
			{"status": "Closed", "first_response_seconds": 30, "closed_at": to_system_datetime(_iso(t - timedelta(minutes=1)))},
		)
		_process(INBOUND, db_id="2", message_id="telegram:2", sent_at=_iso(t))
		conv = frappe.get_doc("DB Conversation", name)
		self.assertEqual(conv.status, "Open")
		self.assertFalse(conv.first_response_seconds)  # Int column: 0 = not measured
		self.assertIsNotNone(conv.opened_at)

	def test_first_response_seconds(self):
		t = datetime.now(timezone.utc).replace(microsecond=0)
		_process(INBOUND, db_id="1", sent_at=_iso(t))
		# outgoing in the same telegram thread
		data = _data(INBOUND)
		from dialogbrain.sync.processor import process_event

		data["message"].update(
			{
				"db_id": "2",
				"message_id": "telegram:2",
				"is_outgoing": True,
				"from_id": "system",
				"from_name": "You",
				"html": "Here it is",
				"sent_at": _iso(t + timedelta(seconds=90)),
			}
		)
		process_event("message.received", data)
		data["message"].update({"db_id": "3", "message_id": "telegram:3", "sent_at": _iso(t + timedelta(seconds=200))})
		process_event("message.received", data)
		conv = frappe.get_doc("DB Conversation", _conv_by_thread("1001"))
		self.assertEqual(conv.first_response_seconds, 90)
		self.assertEqual(conv.last_message_direction, "out")
		self.assertEqual(conv.unread_count, 1)

	# --- other events -------------------------------------------------------

	def test_draft_created_supersedes_previous_pending(self):
		from dialogbrain.sync.processor import process_event

		_process(INBOUND)
		base = {
			"thread_id": "1001",
			"channel_account_id": "155",
			"channel_ref": "telegram:100000001",
			"channel_type": "telegram",
			"created_at": "2026-11-13T12:07:00+00:00",
		}
		process_event("draft.created", {**base, "draft_id": 11, "response_text": "First draft"})
		process_event("draft.created", {**base, "draft_id": 12, "response_text": "Second draft"})
		process_event("draft.created", {**base, "draft_id": 12, "response_text": "Second draft"})
		drafts = {
			d.draft_id: d
			for d in frappe.get_all("DB Draft", fields=["draft_id", "status", "text", "conversation"])
		}
		self.assertEqual(set(drafts), {"11", "12"})
		self.assertEqual(drafts["11"].status, "superseded")
		self.assertEqual(drafts["12"].status, "pending")
		self.assertEqual(drafts["12"].text, "Second draft")
		self.assertEqual(drafts["12"].conversation, _conv_by_thread("1001"))

	def test_handoff_sets_needs_human_and_notifies(self):
		from dialogbrain.sync.processor import process_event

		_process(INBOUND)
		name = _conv_by_thread("1001")
		frappe.db.delete("Notification Log", {"document_type": "DB Conversation"})
		# handoff.requested carries the internal thread id as an int
		process_event("handoff.requested", {"thread_id": 1001, "reason": "pricing", "origin": "agent"})
		self.assertEqual(frappe.db.get_value("DB Conversation", name, "needs_human"), 1)
		self.assertEqual(sorted(_logs(name)), sorted(permitted_users("155")))
		process_event("handoff.requested", {"thread_id": 1001, "reason": "again", "origin": "agent"})
		self.assertEqual(len(_logs(name)), 2 * len(permitted_users("155")))

	def test_handoff_before_conversation_fails_then_retries(self):
		from dialogbrain.sync.processor import process_delivery

		log = frappe.get_doc(
			{
				"doctype": "DB Webhook Log",
				"delivery_id": "h-1",
				"event": "handoff.requested",
				"payload": json.dumps(
					{"event": "handoff.requested", "data": {"thread_id": 1001, "reason": "x", "origin": "agent"}, "delivery_id": "h-1"}
				),
				"status": "queued",
				"attempts": 0,
			}
		).insert(ignore_permissions=True).name
		process_delivery(log_name=log)
		row = frappe.db.get_value("DB Webhook Log", log, ["status", "error"], as_dict=True)
		self.assertEqual(row.status, "failed")
		self.assertIn("unknown thread", row.error)

		_process(INBOUND)
		name = _conv_by_thread("1001")
		frappe.db.delete("Notification Log", {"document_type": "DB Conversation"})
		process_delivery(log_name=log)
		self.assertEqual(frappe.db.get_value("DB Webhook Log", log, "status"), "processed")
		self.assertEqual(frappe.db.get_value("DB Conversation", name, "needs_human"), 1)
		self.assertEqual(sorted(_logs(name)), sorted(permitted_users("155")))

	def test_message_deleted_marks_deleted(self):
		_process(GROUP)
		_process(DELETED)
		self.assertEqual(_msg("9000002").delivery_status, "deleted")

	def test_message_deleted_scoped_to_its_account(self):
		_process(GROUP)
		# same channel ref in another account must not be touched
		data = _data(GROUP, db_id="9000099")
		data["account_id"] = "156"
		data["thread"]["db_id"] = "1099"
		from dialogbrain.sync.processor import process_event

		process_event("message.received", data)
		_process(DELETED)
		self.assertEqual(_msg("9000002").delivery_status, "deleted")
		self.assertNotEqual(_msg("9000099").delivery_status, "deleted")

	def test_contact_updated_relinks(self):
		from dialogbrain.sync.processor import process_event

		_process(INBOUND)
		name = _conv_by_thread("1001")
		frappe.db.set_value("DB Conversation", name, "db_contact_id", "555")
		with patch("dialogbrain.sync.processor.link_party") as link:
			process_event(
				"contact.updated",
				{"contact_id": 555, "workspace_id": 128, "fields": ["display_name"], "display_name": "Alice B.", "source": "api"},
			)
		self.assertEqual(frappe.db.get_value("DB Conversation", name, "contact_name"), "Alice B.")
		link.assert_called_once_with(name)

	def test_conversation_started_creates_conversation(self):
		from dialogbrain.sync.processor import process_event

		process_event(
			"conversation.started",
			{
				"thread_id": 1003,
				"workspace_id": 128,
				"channel": "telegram",
				"account_id": "155",
				"thread_ref": "telegram:100000003",
				"title": "Carol",
				"first_message": {"message_id": "telegram:1", "text": "hi", "from_name": "Carol", "sent_at": None},
			},
		)
		conv = frappe.get_doc("DB Conversation", _conv_by_thread("1003"))
		self.assertEqual(conv.thread_ref, "telegram:100000003")
		self.assertEqual(conv.title, "Carol")
		self.assertEqual(conv.channel, "155")

	def test_conversation_updated_by_peer_updates_title(self):
		from dialogbrain.sync.processor import process_event

		_process(GROUP)
		process_event(
			"conversation.updated",
			{"channel": "telegram", "peer": "-1000000000002", "title": "Renamed Group", "account_id": "155"},
		)
		self.assertEqual(frappe.db.get_value("DB Conversation", _conv_by_thread("1002"), "title"), "Renamed Group")

	def test_unknown_event_ignored(self):
		from dialogbrain.sync.processor import process_event

		process_event("call.ended", {"call_id": 1})
		self.assertEqual(frappe.db.count("DB Conversation"), 0)

	# --- realtime & notifications -------------------------------------------

	def test_realtime_only_to_permitted_users(self):
		frappe.get_doc(
			{"doctype": "DB Channel", "account_id": "155", "channel_type": "telegram", "allowed_users": [{"user": self.agent}]}
		).insert(ignore_permissions=True)
		with patch("frappe.publish_realtime") as pub:
			_process(INBOUND)
		ours = [c for c in pub.call_args_list if str(c.args[0] if c.args else c.kwargs.get("event")).startswith("dialogbrain:")]
		self.assertTrue(ours)
		users = {c.kwargs["user"] for c in ours}
		self.assertEqual(users, {self.agent, self.manager})
		self.assertNotIn(self.other, users)
		for c in ours:
			self.assertTrue(c.kwargs.get("after_commit"))

	def test_notify_unassigned_only_first_inbound(self):
		_process(INBOUND, db_id="1")
		_process(INBOUND, db_id="2", message_id="telegram:2")
		name = _conv_by_thread("1001")
		logs = _logs(name)
		self.assertEqual(sorted(logs), sorted({self.manager, self.agent, self.other}))

	def test_notify_reopened_conversation_again(self):
		_process(INBOUND, db_id="1")
		name = _conv_by_thread("1001")
		frappe.db.set_value("DB Conversation", name, "status", "Closed")
		_process(INBOUND, db_id="2", message_id="telegram:2")
		self.assertEqual(len(_logs(name)), 2 * 3)

	def test_notify_assigned_every_inbound(self):
		_process(INBOUND, db_id="1")
		name = _conv_by_thread("1001")
		frappe.db.delete("Notification Log", {"document_type": "DB Conversation"})
		frappe.db.set_value("DB Conversation", name, "assigned_to", self.agent)
		_process(INBOUND, db_id="2", message_id="telegram:2")
		_process(INBOUND, db_id="3", message_id="telegram:3")
		self.assertEqual(_logs(name), [self.agent, self.agent])

	def test_outgoing_does_not_notify(self):
		_process(OUTGOING)
		self.assertEqual(_logs(_conv_by_thread("8451249")), [])

	def test_skip_push_history_message_is_silent(self):
		"""DialogBrain re-emits fetched channel history as ``message.received`` with
		``skip_push: true`` (seen live: 2022-2025 messages during our backfill). Mirror it,
		but notify nobody and publish nothing."""
		from dialogbrain.sync.processor import process_event

		data = _data(INBOUND, db_id="1")
		data["skip_push"] = True
		with patch("frappe.publish_realtime") as pub:
			process_event("message.received", data)
		name = _conv_by_thread("1001")
		self.assertTrue(_msg("1"))
		self.assertEqual(_logs(name), [])
		self.assertFalse([c for c in pub.call_args_list if str(c.args[0] if c.args else "").startswith("dialogbrain:")])

	def test_silent_upsert_publishes_nothing(self):
		from dialogbrain.sync.upsert import (
			apply_message_counters,
			upsert_channel,
			upsert_conversation,
			upsert_message,
		)

		data = _data(INBOUND)
		with patch("frappe.publish_realtime") as pub:
			channel = upsert_channel("155", "telegram")
			conv, created = upsert_conversation(data["thread"], channel)
			self.assertTrue(created)
			name, created = upsert_message(conv, data["message"], silent=True)
			self.assertTrue(created)
			apply_message_counters(conv, name, silent=True)
		self.assertFalse([c for c in pub.call_args_list if str(c.args[0] if c.args else "").startswith("dialogbrain:")])
		self.assertEqual(_logs(conv), [])
		self.assertEqual(frappe.db.get_value("DB Conversation", conv, "last_message_preview"), data["message"]["html"])

	# --- delivery log -------------------------------------------------------

	def _log(self, fixture):
		body = load_fixture(fixture)
		return frappe.get_doc(
			{
				"doctype": "DB Webhook Log",
				"delivery_id": body["delivery_id"],
				"event": body["event"],
				"payload": json.dumps(body),
				"status": "queued",
				"attempts": 0,
			}
		).insert(ignore_permissions=True).name

	def test_process_delivery_success(self):
		from dialogbrain.sync.processor import process_delivery

		log = self._log(INBOUND)
		process_delivery(log_name=log)
		row = frappe.db.get_value("DB Webhook Log", log, ["status", "attempts", "error"], as_dict=True)
		self.assertEqual((row.status, row.attempts), ("processed", 1))
		self.assertTrue(_msg("9000001"))

	def test_process_delivery_twice_processes_once(self):
		from dialogbrain.sync import processor

		log = self._log(INBOUND)
		processor.process_delivery(log_name=log)
		conv = _msg("9000001").conversation
		notified = len(_logs(conv))
		with patch.object(processor, "process_event") as process_event:
			processor.process_delivery(log_name=log)  # e.g. the webhook job and a retry job
		process_event.assert_not_called()
		self.assertEqual(len(_logs(conv)), notified)
		row = frappe.db.get_value("DB Webhook Log", log, ["status", "attempts"], as_dict=True)
		self.assertEqual((row.status, row.attempts), ("processed", 1))

	def test_process_delivery_commits_attempt_before_processing(self):
		from dialogbrain.sync import processor

		log = self._log(INBOUND)
		order = []

		def commit():
			order.append(("commit", frappe.db.get_value("DB Webhook Log", log, "attempts")))

		with patch.object(processor, "_commit", side_effect=commit), patch.object(
			processor, "process_event", side_effect=lambda *a: order.append(("process", None))
		):
			processor.process_delivery(log_name=log)
		self.assertEqual(order, [("commit", 1), ("process", None)])

	def test_killed_job_retried_until_attempt_cap(self):
		"""A worker killed mid-processing leaves the row queued with the attempt already
		counted; the retry gives up after 5 attempts."""
		from dialogbrain.sync import processor
		from dialogbrain.sync.reconcile import retry_webhook_logs

		class Killed(BaseException):
			pass

		log = self._log(INBOUND)
		frappe.db.set_value("DB Webhook Log", log, "received_at", frappe.utils.now_datetime() - timedelta(minutes=30))
		runs = 0
		for _ in range(10):
			with patch("frappe.enqueue") as enqueue:
				retry_webhook_logs()
			if not enqueue.called:
				break
			self.assertEqual(enqueue.call_args.kwargs["log_name"], log)
			with patch.object(processor, "process_event", side_effect=Killed), self.assertRaises(Killed):
				processor.process_delivery(log_name=log)
			runs += 1
		self.assertEqual(runs, 5)
		row = frappe.db.get_value("DB Webhook Log", log, ["status", "attempts"], as_dict=True)
		self.assertEqual((row.status, row.attempts), ("queued", 5))
		with patch.object(processor, "process_event") as process_event:
			processor.process_delivery(log_name=log)  # a stale job past the cap does nothing
		process_event.assert_not_called()

	def test_processed_delivery_never_reprocessed(self):
		from dialogbrain.sync import processor
		from dialogbrain.sync.reconcile import retry_webhook_logs

		log = self._log(INBOUND)
		processor.process_delivery(log_name=log)
		frappe.db.set_value("DB Webhook Log", log, "received_at", frappe.utils.now_datetime() - timedelta(hours=1))
		with patch("frappe.enqueue") as enqueue:
			retry_webhook_logs()
		enqueue.assert_not_called()
		with patch.object(processor, "process_event") as process_event:
			processor.process_delivery(log_name=log)
		process_event.assert_not_called()

	def test_process_delivery_failure_marks_failed(self):
		from dialogbrain.sync import processor

		log = self._log(INBOUND)
		with patch.object(processor, "process_event", side_effect=ValueError("boom")):
			processor.process_delivery(log_name=log)
		row = frappe.db.get_value("DB Webhook Log", log, ["status", "attempts", "error"], as_dict=True)
		self.assertEqual(row.status, "failed")
		self.assertEqual(row.attempts, 1)
		self.assertIn("boom", row.error)
		processor.process_delivery(log_name=log)
		row = frappe.db.get_value("DB Webhook Log", log, ["status", "attempts"], as_dict=True)
		self.assertEqual((row.status, row.attempts), ("processed", 2))

	def test_process_delivery_deadlock_marks_failed(self):
		"""A deadlock ends the whole transaction, so the savepoint is gone: the delivery is
		still marked failed (retried) instead of the job crashing with the row queued."""
		from dialogbrain.sync import processor

		log = self._log(INBOUND)

		def rollback(*args, save_point=None, **kwargs):
			if save_point:
				raise Exception("(1305, 'SAVEPOINT dialogbrain_delivery does not exist')")

		with patch.object(processor, "process_event", side_effect=frappe.QueryDeadlockError("Deadlock")), patch.object(
			frappe.db, "rollback", side_effect=rollback
		):
			processor.process_delivery(log_name=log)
		row = frappe.db.get_value("DB Webhook Log", log, ["status", "error"], as_dict=True)
		self.assertEqual(row.status, "failed")
		self.assertIn("Deadlock", row.error)

	def test_process_delivery_failure_rolls_back_partial_writes(self):
		from dialogbrain.sync import processor

		log = self._log(INBOUND)
		with patch.object(processor, "apply_message_counters", side_effect=ValueError("late boom")):
			processor.process_delivery(log_name=log)
		self.assertEqual(frappe.db.get_value("DB Webhook Log", log, "status"), "failed")
		self.assertIsNone(_msg("9000001"))
