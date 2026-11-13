from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from dialogbrain.api import conversations as api
from dialogbrain.permissions import is_manager
from dialogbrain.tests.utils import DialogBrainTestCase

CH_A = "inbox-a"
CH_B = "inbox-b"


def _user(email, *roles):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	user = frappe.get_doc(
		{"doctype": "User", "email": email, "first_name": email.split("@")[0], "enabled": 1,
		 "send_welcome_email": 0}
	)
	user.insert(ignore_permissions=True)
	user.add_roles(*roles)
	return email


def _channel(account_id, channel_type, users=()):
	return (
		frappe.get_doc(
			{
				"doctype": "DB Channel",
				"account_id": account_id,
				"channel_type": channel_type,
				"title": f"Title {account_id}",
				"allowed_users": [{"user": u} for u in users],
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


_seq = [0]


def _conv(channel, **fields):
	_seq[0] += 1
	doc = {"doctype": "DB Conversation", "thread_id": f"inbox-t{_seq[0]}", "channel": channel}
	doc.update(fields)
	return frappe.get_doc(doc).insert(ignore_permissions=True).name


def _msg(conversation, text="hi", direction="in", sent_at=None, **fields):
	_seq[0] += 1
	doc = {
		"doctype": "DB Message",
		"conversation": conversation,
		"message_id": f"inbox-m{_seq[0]}",
		"direction": direction,
		"author_type": "customer" if direction == "in" else "manager",
		"text": text,
		"sent_at": sent_at or now_datetime(),
	}
	doc.update(fields)
	return frappe.get_doc(doc).insert(ignore_permissions=True).name


def _names(result):
	return {r["name"] for r in result["rows"]}


class TestInboxApi(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete("DB Draft")
		frappe.db.delete("DB Message Attachment")
		frappe.db.delete("DB Message")
		frappe.db.delete("DB Conversation")
		frappe.db.delete("DB Channel User")
		frappe.db.delete("DB Channel")
		frappe.db.set_single_value("DialogBrain Settings", "agent_capabilities", None)  # Plan 1 baseline
		self.agent_a = _user("inbox-agent-a@example.com", "Chat Agent")
		self.agent_b = _user("inbox-agent-b@example.com", "Chat Agent")
		self.mgr = _user("inbox-mgr@example.com", "Chat Manager")
		_channel(CH_A, "telegram")
		_channel(CH_B, "whatsapp", [self.agent_b])
		base = now_datetime() - timedelta(days=1)
		self.base = base
		# Channel A
		self.a_open_unassigned = _conv(
			CH_A, title="Alice", phone="+15550001111", last_message_at=base,
			last_message_direction="in", unread_count=2, last_message_preview="hello there",
		)
		self.a_open_mine = _conv(
			CH_A, title="Bob", assigned_to=self.agent_a, last_message_at=base - timedelta(minutes=1),
			last_message_direction="out", needs_human=1,
		)
		self.a_closed = _conv(
			CH_A, title="Carol", status="Closed", last_message_at=base - timedelta(minutes=2),
			last_message_direction="in",
		)
		# Channel B (restricted to agent_b)
		self.b_open = _conv(
			CH_B, title="Dave", assigned_to=self.agent_b, last_message_at=base - timedelta(minutes=3),
			last_message_direction="in", unread_count=1,
		)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	# --- list -------------------------------------------------------------

	def test_list_scopes_and_filters(self):
		frappe.set_user(self.agent_a)
		lc = api.list_conversations
		self.assertEqual(_names(lc()), {self.a_open_unassigned, self.a_open_mine})
		self.assertEqual(_names(lc({"status": "Closed"})), {self.a_closed})
		self.assertEqual(
			_names(lc({"status": "All"})), {self.a_open_unassigned, self.a_open_mine, self.a_closed}
		)
		self.assertEqual(_names(lc({"scope": "mine"})), {self.a_open_mine})
		self.assertEqual(_names(lc({"scope": "unassigned"})), {self.a_open_unassigned})
		self.assertEqual(_names(lc({"unanswered": 1})), {self.a_open_unassigned})
		# unanswered means Open even when status=All
		self.assertEqual(_names(lc({"unanswered": True, "status": "All"})), {self.a_open_unassigned})
		self.assertEqual(_names(lc({"channel": CH_A})), {self.a_open_unassigned, self.a_open_mine})
		self.assertEqual(_names(lc({"channel_type": "telegram"})), {self.a_open_unassigned, self.a_open_mine})
		self.assertEqual(_names(lc({"channel_type": "whatsapp"})), set())
		# assigned_to is ignored for agents
		self.assertEqual(
			_names(lc({"assigned_to": self.agent_b})), {self.a_open_unassigned, self.a_open_mine}
		)
		# JSON string form (as posted by the browser)
		self.assertEqual(_names(lc('{"scope": "mine"}')), {self.a_open_mine})

		frappe.set_user(self.mgr)
		self.assertEqual(
			_names(lc()), {self.a_open_unassigned, self.a_open_mine, self.b_open}
		)
		self.assertEqual(_names(lc({"assigned_to": self.agent_b})), {self.b_open})
		self.assertEqual(_names(lc({"channel_type": "whatsapp"})), {self.b_open})
		self.assertEqual(_names(lc({"unanswered": 1})), {self.a_open_unassigned, self.b_open})

		row = next(r for r in lc()["rows"] if r["name"] == self.a_open_unassigned)
		for key in (
			"name", "title", "contact_name", "channel", "channel_type", "channel_title", "avatar_url",
			"assigned_to", "assigned_to_name", "status", "unread_count", "needs_human",
			"last_message_at", "last_message_preview", "last_message_direction", "party_type", "party",
		):
			self.assertIn(key, row)
		self.assertEqual(row["channel_type"], "telegram")
		self.assertEqual(row["channel_title"], f"Title {CH_A}")
		mine = next(r for r in lc()["rows"] if r["name"] == self.a_open_mine)
		self.assertEqual(mine["assigned_to_name"], "inbox-agent-a")

	def test_list_paging_keyset(self):
		frappe.db.delete("DB Conversation")
		start = now_datetime() - timedelta(days=10)
		for i in range(120):
			if i % 6 == 0:
				_conv(CH_A)  # no messages yet: sorted by creation
			else:
				# a few share the same timestamp to exercise the name tie-break
				_conv(CH_A, last_message_at=start + timedelta(minutes=i // 2))
		rows = frappe.db.sql(
			"""SELECT name, COALESCE(last_message_at, creation) AS k FROM `tabDB Conversation`""",
			as_dict=True,
		)
		expected = [r.name for r in sorted(rows, key=lambda r: (get_datetime(r.k), r.name), reverse=True)]

		frappe.set_user(self.agent_a)
		got, cursor, pages = [], None, 0
		while True:
			res = api.list_conversations({"status": "All"}, cursor=cursor, limit=50)
			got.extend(r["name"] for r in res["rows"])
			pages += 1
			cursor = res["next_cursor"]
			if not cursor:
				break
			cursor = frappe.as_json(cursor)  # browser sends it back as JSON
			self.assertLess(pages, 5)
		self.assertEqual(pages, 3)
		self.assertEqual(got, expected)

	def test_list_search_by_phone_and_text(self):
		frappe.set_user(self.mgr)
		self.assertEqual(_names(api.list_conversations({"search": "0001111"})), {self.a_open_unassigned})
		self.assertEqual(_names(api.list_conversations({"search": "Bob"})), {self.a_open_mine})
		_msg(self.b_open, text="<p>invoice number ZX-778</p>")
		old = _msg(self.a_open_mine, text="ancient QQ-999", sent_at=now_datetime() - timedelta(days=200))
		self.assertTrue(old)
		self.assertEqual(_names(api.list_conversations({"search": "ZX-778"})), {self.b_open})
		# older than 90 days: not matched by message text
		self.assertEqual(_names(api.list_conversations({"search": "QQ-999"})), set())
		# < 3 chars: no message search, LIKE wildcards are literal
		self.assertEqual(_names(api.list_conversations({"search": "%"})), set())
		self.assertEqual(_names(api.list_conversations({"search": "' OR 1=1 -- "})), set())
		# agent_a cannot find the channel-B conversation through message text
		frappe.set_user(self.agent_a)
		self.assertEqual(_names(api.list_conversations({"search": "ZX-778"})), set())

	# --- access -----------------------------------------------------------

	def test_agent_cannot_see_restricted_channel(self):
		msg = _msg(self.b_open, text="secret")
		frappe.set_user(self.agent_a)
		self.assertNotIn(self.b_open, _names(api.list_conversations({"status": "All"})))
		calls = (
			lambda: api.list_conversations({"channel": CH_B}),
			lambda: api.get_conversation(self.b_open),
			lambda: api.get_messages(self.b_open),
			lambda: api.get_messages_by_name(self.b_open, [msg]),
			lambda: api.mark_read(self.b_open),
			lambda: api.assign(self.b_open, self.agent_a),
			lambda: api.set_status(self.b_open, "Closed"),
			lambda: api.get_drafts(self.b_open),
		)
		for call in calls:
			with self.assertRaises(frappe.PermissionError):
				call()
		frappe.set_user("Administrator")
		conv = frappe.db.get_value(
			"DB Conversation", self.b_open, ["unread_count", "assigned_to", "status"], as_dict=True
		)
		self.assertEqual((conv.unread_count, conv.assigned_to, conv.status), (1, self.agent_b, "Open"))

	def test_agent_assign_rules(self):
		frappe.set_user(self.agent_a)
		# self on unassigned -> ok
		api.assign(self.a_open_unassigned, self.agent_a)
		self.assertEqual(frappe.db.get_value("DB Conversation", self.a_open_unassigned, "assigned_to"), self.agent_a)
		# self on someone else's -> refused
		frappe.db.set_value("DB Conversation", self.a_closed, "assigned_to", self.agent_b)
		with self.assertRaises(frappe.PermissionError):
			api.assign(self.a_closed, self.agent_a)
		# unassigning someone else's -> refused
		with self.assertRaises(frappe.PermissionError):
			api.assign(self.a_closed, None)
		# assigning a colleague -> refused (even on an unassigned conversation)
		frappe.db.set_value("DB Conversation", self.a_open_unassigned, "assigned_to", None)
		with self.assertRaises(frappe.PermissionError):
			api.assign(self.a_open_unassigned, self.agent_b)
		self.assertFalse(frappe.db.get_value("DB Conversation", self.a_open_unassigned, "assigned_to"))
		self.assertEqual(frappe.db.get_value("DB Conversation", self.a_closed, "assigned_to"), self.agent_b)
		# unassigning their own -> ok
		api.assign(self.a_open_mine, None)
		self.assertFalse(frappe.db.get_value("DB Conversation", self.a_open_mine, "assigned_to"))

	def test_manager_assigns_anyone(self):
		frappe.set_user(self.mgr)
		self.assertTrue(is_manager())
		api.assign(self.a_open_mine, self.agent_b)
		self.assertEqual(frappe.db.get_value("DB Conversation", self.a_open_mine, "assigned_to"), self.agent_b)
		api.assign(self.a_open_mine, None)
		self.assertFalse(frappe.db.get_value("DB Conversation", self.a_open_mine, "assigned_to"))
		api.assign(self.b_open, self.mgr)
		self.assertEqual(frappe.db.get_value("DB Conversation", self.b_open, "assigned_to"), self.mgr)
		# agent_a cannot see channel B -> refused
		with self.assertRaises(frappe.ValidationError):
			api.assign(self.b_open, self.agent_a)
		# a user without a chat role -> refused
		plain = _user("inbox-plain@example.com")
		with self.assertRaises(frappe.ValidationError):
			api.assign(self.a_open_mine, plain)
		self.assertEqual(frappe.db.get_value("DB Conversation", self.b_open, "assigned_to"), self.mgr)
		self.assertTrue(api.get_conversation(self.b_open)["permissions"]["can_assign_any"])
		frappe.set_user(self.agent_a)
		self.assertFalse(is_manager())
		self.assertFalse(api.get_conversation(self.a_open_mine)["permissions"]["can_assign_any"])

	def test_needs_human_filter_and_channel_types(self):
		frappe.set_user(self.mgr)
		self.assertEqual(_names(api.list_conversations({"needs_human": 1})), {self.a_open_mine})
		self.assertEqual(api.channel_types(), ["telegram", "whatsapp"])
		frappe.set_user(self.agent_a)
		self.assertEqual(api.channel_types(), ["telegram"])

	def test_assignable_users(self):
		frappe.set_user(self.mgr)
		names = [u["name"] for u in api.assignable_users(self.a_open_mine)]
		self.assertIn(self.agent_a, names)
		self.assertIn(self.agent_b, names)
		self.assertIn(self.mgr, names)
		# channel B is restricted to agent_b: agent_a is not assignable there
		names_b = [u["name"] for u in api.assignable_users(self.b_open)]
		self.assertIn(self.agent_b, names_b)
		self.assertNotIn(self.agent_a, names_b)
		self.assertTrue(all(u["full_name"] for u in api.assignable_users(self.b_open)))
		# manager filter list (no conversation): every chat user
		all_names = [u["name"] for u in api.assignable_users()]
		self.assertTrue({self.agent_a, self.agent_b, self.mgr} <= set(all_names))
		# agent: only themself, and nothing for the manager filter
		frappe.set_user(self.agent_a)
		self.assertEqual([u["name"] for u in api.assignable_users(self.a_open_mine)], [self.agent_a])
		self.assertEqual(api.assignable_users(), [])
		# agent: a conversation on a channel they cannot access -> refused
		with self.assertRaises(frappe.PermissionError):
			api.assignable_users(self.b_open)

	# --- actions ----------------------------------------------------------

	def test_close_and_reopen_set_timestamps(self):
		frappe.db.set_value("DB Conversation", self.a_open_unassigned, "first_response_seconds", 42)
		frappe.set_user(self.agent_a)
		api.set_status(self.a_open_unassigned, "Closed")
		conv = frappe.db.get_value(
			"DB Conversation", self.a_open_unassigned, ["status", "closed_at"], as_dict=True
		)
		self.assertEqual(conv.status, "Closed")
		self.assertTrue(conv.closed_at)
		api.set_status(self.a_open_unassigned, "Open")
		conv = frappe.db.get_value(
			"DB Conversation",
			self.a_open_unassigned,
			["status", "closed_at", "opened_at", "first_response_seconds"],
			as_dict=True,
		)
		self.assertEqual(conv.status, "Open")
		self.assertEqual(conv.first_response_seconds, 0)  # R1: "not measured yet"
		self.assertIsNone(conv.closed_at)
		self.assertGreater(get_datetime(conv.opened_at), now_datetime() - timedelta(minutes=1))
		with self.assertRaises(frappe.ValidationError):
			api.set_status(self.a_open_unassigned, "Bogus")

	def test_close_clears_needs_human(self):
		frappe.set_user(self.agent_a)
		api.set_status(self.a_open_mine, "Closed")
		self.assertEqual(frappe.db.get_value("DB Conversation", self.a_open_mine, "needs_human"), 0)

	def test_mark_read_publishes_only_when_unread(self):
		"""R2: write + publish only when unread_count > 0."""
		frappe.set_user(self.agent_a)
		with patch("dialogbrain.api.conversations.publish") as pub:
			api.mark_read(self.a_open_unassigned)  # unread_count 2
			self.assertEqual(pub.call_count, 1)
			self.assertEqual(pub.call_args.args[:2], (self.a_open_unassigned, "conversation"))
		with patch("dialogbrain.api.conversations.publish") as pub:
			api.mark_read(self.a_open_unassigned)  # now 0
			api.mark_read(self.a_open_mine)  # 0 from the start
			pub.assert_not_called()

	def test_assign_race_loser_refused(self):
		"""R4: another agent takes the conversation between our check and our write."""
		real = api.permitted_users

		def colleague_wins(channel):
			frappe.db.sql(
				"UPDATE `tabDB Conversation` SET assigned_to = %s WHERE name = %s",
				(self.agent_b, self.a_open_unassigned),
			)
			return real(channel)

		frappe.set_user(self.agent_a)
		with patch("dialogbrain.api.conversations.permitted_users", side_effect=colleague_wins):
			with self.assertRaises(frappe.PermissionError):
				api.assign(self.a_open_unassigned, self.agent_a)
		self.assertEqual(
			frappe.db.get_value("DB Conversation", self.a_open_unassigned, "assigned_to"), self.agent_b
		)

		# unassign race: a manager reassigns our conversation before our write
		def manager_reassigns():
			frappe.db.sql(
				"UPDATE `tabDB Conversation` SET assigned_to = %s WHERE name = %s",
				(self.agent_b, self.a_open_mine),
			)

		orig_get_value = frappe.db.get_value

		def get_value(*args, **kwargs):
			out = orig_get_value(*args, **kwargs)
			if kwargs.get("for_update") and args[1] == self.a_open_mine:
				manager_reassigns()
			return out

		with patch.object(frappe.db, "get_value", side_effect=get_value):
			with self.assertRaises(frappe.PermissionError):
				api.assign(self.a_open_mine, None)
		self.assertEqual(frappe.db.get_value("DB Conversation", self.a_open_mine, "assigned_to"), self.agent_b)

	def test_mark_read_zeroes_unread(self):
		frappe.set_user(self.agent_a)
		api.mark_read(self.a_open_unassigned)
		self.assertEqual(frappe.db.get_value("DB Conversation", self.a_open_unassigned, "unread_count"), 0)

	# --- messages ---------------------------------------------------------

	def test_messages_ordered_by_sent_at_and_paged(self):
		base = now_datetime() - timedelta(hours=5)
		order = [3, 0, 4, 1, 2]  # insert out of order
		names = {}
		for i in order:
			names[i] = _msg(self.a_open_mine, text=f"m{i}", sent_at=base + timedelta(minutes=i))
		frappe.set_user(self.agent_a)
		page1 = api.get_messages(self.a_open_mine, limit=3)
		self.assertEqual([r["name"] for r in page1["rows"]], [names[4], names[3], names[2]])
		self.assertTrue(page1["next_before"])
		page2 = api.get_messages(self.a_open_mine, before=frappe.as_json(page1["next_before"]), limit=3)
		self.assertEqual([r["name"] for r in page2["rows"]], [names[1], names[0]])
		self.assertIsNone(page2["next_before"])

	def test_message_links_open_in_new_tab(self):
		html = api.sanitize_message_html(
			'<p>see <a href="https://example.com/x">this</a> and <a href="/app/x" target="_self" rel="opener">that</a></p>'
		)
		from bs4 import BeautifulSoup

		links = BeautifulSoup(html, "html.parser").find_all("a")
		self.assertEqual(len(links), 2)
		for a in links:
			self.assertEqual(a.get("target"), "_blank")
			self.assertEqual(sorted(a.get("rel")), ["noopener", "noreferrer"])
		self.assertEqual(links[0]["href"], "https://example.com/x")
		self.assertEqual(api.sanitize_message_html("<p>plain</p>"), "<p>plain</p>")

	def test_message_html_sanitised(self):
		evil = "<p>hi</p><script>x</script><img src=x onerror=y>"
		name = _msg(self.a_open_mine, text=evil)
		inert = _msg(self.a_open_mine, text="click javascript:alert(1)", sent_at=now_datetime() - timedelta(hours=1))
		frappe.db.set_value(
			"DB Conversation", self.a_open_mine, "last_message_preview", "<b>x</b>&lt;script&gt;y"
		)
		frappe.set_user(self.agent_a)
		rows = {r["name"]: r for r in api.get_messages(self.a_open_mine)["rows"]}
		html = rows[name]["html"]
		self.assertIn("<p>hi</p>", html)
		self.assertNotIn("<script", html.lower())
		self.assertNotIn("onerror", html.lower())
		self.assertNotIn("<", rows[name]["text"])
		self.assertNotIn("<a", rows[inert]["html"])
		self.assertNotIn("href", rows[inert]["html"])
		preview = next(
			r for r in api.list_conversations()["rows"] if r["name"] == self.a_open_mine
		)["last_message_preview"]
		self.assertNotIn("<", preview)
		self.assertNotIn(">", preview)
		self.assertEqual(api.get_conversation(self.a_open_mine)["last_message_preview"], preview)

	def test_message_html_drops_forms_meta_and_styles(self):
		"""R3: no phishing forms, no link/meta, no style attributes (overlays)."""
		evil = (
			'<link rel="stylesheet" href="https://evil.example/x.css">'
			'<meta http-equiv="refresh" content="0;url=https://evil.example">'
			'<div style="position:fixed;top:0;left:0;width:100%;height:100%;z-index:9999">'
			'<form action="https://evil.example/login" method="post">'
			'<input name="password" type="password"><select><option>a</option></select>'
			'<textarea>t</textarea><button type="submit">Log in</button></form></div>'
			'<iframe src="https://evil.example"></iframe><base href="https://evil.example/">'
			'<p style="color:red">keep me</p><img src="https://cdn.example/a.png">'
		)
		_msg(self.a_open_mine, text=evil)
		frappe.set_user(self.agent_a)
		html = api.get_messages(self.a_open_mine)["rows"][0]["html"].lower()
		for bad in ("<form", "<input", "<button", "<select", "<option", "<textarea", "<link",
					"<meta", "<iframe", "<base", "style=", "position:fixed"):
			self.assertNotIn(bad, html, bad)
		self.assertIn("keep me", html)
		self.assertIn('<img src="https://cdn.example/a.png"', html)

	def test_message_rows_carry_client_ref(self):
		name = _msg(
			self.a_open_mine, text="reply", direction="out", client_ref="cref-1",
			delivery_status="failed", error="boom", sent_by=self.agent_a,
			attachments=[{"file_id": "42", "filename": "a.png", "mime_type": "image/png",
						  "media_type": "image", "size": 10}],
		)
		frappe.set_user(self.agent_a)
		row = api.get_messages(self.a_open_mine)["rows"][0]
		self.assertEqual(row["name"], name)
		self.assertEqual(row["client_ref"], "cref-1")
		self.assertEqual(row["delivery_status"], "failed")
		self.assertEqual(row["error"], "boom")
		self.assertEqual(row["sent_by_name"], "inbox-agent-a")
		self.assertEqual(row["direction"], "out")
		self.assertEqual(row["author_type"], "manager")
		self.assertEqual(
			row["attachments"],
			[{"file_id": "42", "filename": "a.png", "mime_type": "image/png", "media_type": "image", "size": 10}],
		)
		self.assertEqual(api.get_messages_by_name(self.a_open_mine, [name]), [row])
		self.assertEqual(api.get_messages_by_name(self.a_open_mine, frappe.as_json([name])), [row])

	def test_unknown_in_flight_flag(self):
		# the sender claims a reply (pending -> unknown, empty error) while the request is
		# in flight; the UI shows it as "sending", not as "delivery unknown" (live e2e)
		fresh = _msg(self.a_open_mine, text="r1", direction="out", client_ref="c-fresh",
					 delivery_status="unknown", sent_by=self.agent_a)
		ambiguous = _msg(self.a_open_mine, text="r2", direction="out", client_ref="c-amb",
						 delivery_status="unknown", error="timed out", sent_by=self.agent_a)
		stale = _msg(self.a_open_mine, text="r3", direction="out", client_ref="c-stale",
					 delivery_status="unknown", sent_by=self.agent_a)
		sent = _msg(self.a_open_mine, text="r4", direction="out", client_ref="c-sent",
					delivery_status="sent", sent_by=self.agent_a)
		old = add_to_date(now_datetime(), seconds=-(api.IN_FLIGHT_SECONDS + 5))
		frappe.db.set_value("DB Message", stale, "modified", old, update_modified=False)
		frappe.set_user(self.agent_a)
		rows = {r["name"]: r for r in api.get_messages(self.a_open_mine)["rows"]}
		self.assertTrue(rows[fresh]["in_flight"])
		self.assertFalse(rows[ambiguous]["in_flight"])
		self.assertFalse(rows[stale]["in_flight"])
		self.assertFalse(rows[sent]["in_flight"])
		self.assertEqual(rows[fresh]["delivery_status"], "unknown")  # the contract is unchanged

	def test_get_messages_by_name_rejects_foreign_names(self):
		mine = _msg(self.a_open_mine, text="a")
		other_a = _msg(self.a_open_unassigned, text="b")
		other_b = _msg(self.b_open, text="c")
		frappe.set_user(self.agent_a)
		for foreign in (other_a, other_b):
			with self.assertRaises(frappe.PermissionError):
				api.get_messages_by_name(self.a_open_mine, [mine, foreign])

	def test_get_messages_by_name_skips_missing_names(self):
		# realtime batches can name a row deleted meanwhile (echo fold): skipped, not refused
		mine = _msg(self.a_open_mine, text="a")
		frappe.set_user(self.agent_a)
		rows = api.get_messages_by_name(self.a_open_mine, [mine, "does-not-exist"])
		self.assertEqual([r["name"] for r in rows], [mine])
		self.assertEqual(api.get_messages_by_name(self.a_open_mine, ["does-not-exist"]), [])
		# access to the conversation is still checked first
		frappe.set_user(self.agent_a)
		with self.assertRaises(frappe.PermissionError):
			api.get_messages_by_name(self.b_open, ["does-not-exist"])

	# --- drafts / counters ------------------------------------------------

	def test_drafts_truncated_flag(self):
		t0 = now_datetime() - timedelta(hours=1)

		def draft(text, minutes, status="pending"):
			_seq[0] += 1
			return frappe.get_doc(
				{"doctype": "DB Draft", "draft_id": f"inbox-d{_seq[0]}", "conversation": self.a_open_mine,
				 "text": text, "created_at": t0 + timedelta(minutes=minutes), "status": status}
			).insert(ignore_permissions=True).name

		short = draft("short", 0)
		webhook = draft("x" * 500, 1)
		listed = draft("y" * 297 + "...", 2)
		dots_short = draft("ends with...", 3)
		draft("old", 4, status="rejected")
		frappe.set_user(self.agent_a)
		rows = api.get_drafts(self.a_open_mine)
		self.assertEqual([r["name"] for r in rows], [dots_short, listed, webhook, short])
		flags = {r["name"]: r["truncated"] for r in rows}
		self.assertEqual(flags, {short: False, webhook: True, listed: True, dots_short: False})
		self.assertEqual(set(rows[0]), {"name", "draft_id", "text", "created_at", "truncated"})

	def test_counters_match_list(self):
		for user in (self.agent_a, self.agent_b, self.mgr):
			frappe.set_user(user)
			c = api.counters()
			rows = api.list_conversations(limit=500)["rows"]
			self.assertEqual(c["unread_total"], sum(r["unread_count"] or 0 for r in rows), user)
			self.assertEqual(c["unanswered"], len(api.list_conversations({"unanswered": 1})["rows"]), user)
			self.assertEqual(c["mine"], len(api.list_conversations({"scope": "mine"})["rows"]), user)
			self.assertEqual(c["unassigned"], len(api.list_conversations({"scope": "unassigned"})["rows"]), user)
			self.assertEqual(c["needs_human"], sum(1 for r in rows if r["needs_human"]), user)
		frappe.set_user(self.agent_a)
		self.assertEqual(
			api.counters(), {"unread_total": 2, "unanswered": 1, "mine": 1, "unassigned": 1, "needs_human": 1}
		)
		frappe.set_user(self.mgr)
		self.assertEqual(api.counters()["unread_total"], 3)

	def test_list_party_filter(self):
		# party fields are set directly: the filter only reads them (no Customer/Lead docs needed)
		frappe.db.set_value("DB Conversation", self.a_open_unassigned, {"party_type": "Customer", "party": "Cust-X"})
		frappe.db.set_value("DB Conversation", self.a_closed, {"party_type": "Customer", "party": "Cust-X"})
		frappe.db.set_value("DB Conversation", self.a_open_mine, {"party_type": "Lead", "party": "Cust-X"})
		frappe.db.set_value("DB Conversation", self.b_open, {"party_type": "Customer", "party": "Cust-X"})
		lc = api.list_conversations
		frappe.set_user(self.mgr)
		party = {"party_type": "Customer", "party": "Cust-X", "status": "All"}
		self.assertEqual(_names(lc(party)), {self.a_open_unassigned, self.a_closed, self.b_open})
		self.assertEqual(
			_names(lc({"party_type": "Lead", "party": "Cust-X", "status": "All"})), {self.a_open_mine}
		)
		self.assertEqual(_names(lc({**party, "party": "Other"})), set())
		# the channel restriction still applies: agent_a cannot see channel B's conversation
		frappe.set_user(self.agent_a)
		self.assertEqual(_names(lc(party)), {self.a_open_unassigned, self.a_closed})
		# both keys are required together, and only Customer / Lead are accepted
		with self.assertRaises(frappe.ValidationError):
			lc({"party": "Cust-X"})
		with self.assertRaises(frappe.ValidationError):
			lc({"party_type": "Customer"})
		with self.assertRaises(frappe.ValidationError):
			lc({"party_type": "User", "party": "Administrator"})

	def test_list_name_filter_refreshes_one_row(self):
		# realtime row refresh: one conversation, under the list's current filters
		lc = api.list_conversations
		frappe.set_user(self.agent_a)
		self.assertEqual(_names(lc({"name": self.a_open_mine})), {self.a_open_mine})
		self.assertEqual(_names(lc({"name": self.a_open_mine, "scope": "unassigned"})), set())
		self.assertEqual(_names(lc({"name": self.a_closed})), set())  # status Open by default
		# another channel's conversation is not returned
		self.assertEqual(_names(lc({"name": self.b_open, "status": "All"})), set())
		# a list of names: one call for a batch of realtime row refreshes
		self.assertEqual(
			_names(lc({"name": [self.a_open_mine, self.a_open_unassigned, self.a_closed, self.b_open]})),
			{self.a_open_mine, self.a_open_unassigned},
		)
		self.assertEqual(_names(lc({"name": []})), {self.a_open_mine, self.a_open_unassigned})
		with self.assertRaises(frappe.ValidationError):
			lc({"name": [{"x": 1}]})

	# --- take over / give back (Plan 3 B4) ---------------------------------------------

	def _caps(self, **caps):
		import json

		frappe.db.set_single_value("DialogBrain Settings", "agent_capabilities", json.dumps(caps))

	def _agent_call(self, fn, conversation, result=None, error=None):
		from unittest.mock import MagicMock

		client = MagicMock()
		if error is not None:
			client.execute.side_effect = error
		else:
			client.execute.return_value = result
		with patch("dialogbrain.api.conversations.DialogBrainClient") as cls, patch(
			"dialogbrain.api.conversations.publish"
		) as pub:
			cls.from_settings.return_value = client
			out = fn(conversation)
		return client, pub, out

	def test_take_over_pauses_agent_and_sets_override(self):
		self._caps(agent_pause=True)
		thread = frappe.db.get_value("DB Conversation", self.a_open_mine, "thread_id")
		frappe.set_user(self.agent_a)
		client, pub, out = self._agent_call(
			api.take_over, self.a_open_mine,
			result={"thread_id": 5, "paused": True, "paused_at": "2026-11-14T10:00:00+00:00"},
		)
		client.execute.assert_called_once_with("threads.set_agent_paused", {"thread_id": thread, "paused": True})
		self.assertEqual(frappe.db.get_value("DB Conversation", self.a_open_mine, "agent_override"), "human")
		self.assertEqual(out["agent_override"], "human")
		self.assertEqual(out["agent"], {"paused": True, "paused_by": "operator", "paused_at": "2026-11-14T10:00:00+00:00"})
		self.assertEqual(pub.call_args[0][:2], (self.a_open_mine, "conversation"))

	def test_give_back_resumes_agent_and_sets_override(self):
		self._caps(agent_pause=True)
		frappe.db.set_value("DB Conversation", self.a_open_mine, "agent_override", "human")
		frappe.set_user(self.agent_a)
		client, pub, out = self._agent_call(
			api.give_back, self.a_open_mine, result={"thread_id": 5, "paused": False, "paused_at": None}
		)
		self.assertEqual(client.execute.call_args[0][1]["paused"], False)
		self.assertEqual(frappe.db.get_value("DB Conversation", self.a_open_mine, "agent_override"), "agent")
		self.assertEqual(out["agent"]["paused"], False)
		pub.assert_called_once()

	def test_take_over_by_channel_ref_when_thread_id_unknown(self):
		self._caps(agent_pause=True)
		frappe.db.set_value("DB Conversation", self.a_open_mine, {"thread_id": None, "thread_ref": "telegram:42"})
		frappe.set_user(self.agent_a)
		client, _, _ = self._agent_call(api.take_over, self.a_open_mine, result={"paused": True})
		self.assertEqual(client.execute.call_args[0][1]["thread_id"], "telegram:42")

	def test_take_over_needs_access(self):
		self._caps(agent_pause=True)
		frappe.set_user(self.agent_a)  # channel B is restricted to agent_b
		for fn in (api.take_over, api.give_back, api.agent_status):
			with self.assertRaises(frappe.PermissionError):
				self._agent_call(fn, self.b_open, result={"paused": True})
		self.assertEqual(frappe.db.get_value("DB Conversation", self.b_open, "agent_override"), "default")

	def test_take_over_without_capability_is_a_clear_error(self):
		self._caps(agent_pause=False)
		frappe.set_user(self.agent_a)
		for fn in (api.take_over, api.give_back):
			with self.assertRaises(frappe.ValidationError) as cm:
				client, _, _ = self._agent_call(fn, self.a_open_mine, result={"paused": True})
			self.assertIn("doesn't support this yet", str(cm.exception))
		self.assertEqual(frappe.db.get_value("DB Conversation", self.a_open_mine, "agent_override"), "default")

	def test_take_over_dialogbrain_error_changes_nothing(self):
		from dialogbrain.client import DialogBrainError

		self._caps(agent_pause=True)
		frappe.set_user(self.agent_a)
		with patch("frappe.log_error"), self.assertRaises(frappe.ValidationError) as cm:
			self._agent_call(
				api.take_over, self.a_open_mine,
				error=DialogBrainError("needs the member role db_live_secret0000", error_type="permission_denied"),
			)
		self.assertNotIn("db_live_secret0000", str(cm.exception))
		self.assertEqual(frappe.db.get_value("DB Conversation", self.a_open_mine, "agent_override"), "default")

	def test_agent_status(self):
		self._caps(agent_pause=True)
		frappe.set_user(self.agent_a)
		client, _, out = self._agent_call(
			api.agent_status, self.a_open_mine,
			result={"thread_id": 5, "paused": True, "paused_by": None, "paused_at": "2026-11-14T10:00:00+00:00"},
		)
		client.execute.assert_called_once()
		self.assertEqual(client.execute.call_args[0][0], "threads.get_agent_status")
		self.assertEqual(
			out, {"supported": True, "paused": True, "paused_by": None, "paused_at": "2026-11-14T10:00:00+00:00"}
		)

	def test_agent_status_without_capability_or_with_dialogbrain_down(self):
		from dialogbrain.client import DialogBrainError

		self._caps(agent_pause=False)
		frappe.set_user(self.agent_a)
		client, _, out = self._agent_call(api.agent_status, self.a_open_mine, result={})
		self.assertEqual(out, {"supported": False})
		client.execute.assert_not_called()
		self._caps(agent_pause=True)
		_, _, out = self._agent_call(api.agent_status, self.a_open_mine, error=DialogBrainError("HTTP 503", status=503))
		self.assertTrue(out["supported"])
		self.assertTrue(out["error"])

	def test_get_conversation_exposes_capabilities(self):
		self._caps(agent_pause=True, full_drafts=False, request_id=True)
		frappe.set_user(self.agent_a)
		perms = api.get_conversation(self.a_open_mine)["permissions"]
		self.assertEqual((perms["agent_pause"], perms["full_drafts"]), (True, False))

	def test_agent_status_uses_a_short_timeout_and_a_10s_cache(self):
		self._caps(agent_pause=True)
		frappe.cache().delete_value(f"dialogbrain_agent_status:{self.a_open_mine}")
		frappe.set_user(self.agent_a)
		state = {"paused": True, "paused_by": "operator", "paused_at": "2026-11-14T10:00:00+00:00"}
		client, _, first = self._agent_call(api.agent_status, self.a_open_mine, result=state)
		self.assertEqual(client.timeout, 5)
		client2, _, second = self._agent_call(api.agent_status, self.a_open_mine, result={"paused": False})
		client2.execute.assert_not_called()
		self.assertEqual(second, first)
		# a take-over / give-back clears it
		self._agent_call(api.give_back, self.a_open_mine, result={"paused": False})
		client3, _, third = self._agent_call(api.agent_status, self.a_open_mine, result={"paused": False})
		client3.execute.assert_called_once()
		self.assertFalse(third["paused"])
		frappe.cache().delete_value(f"dialogbrain_agent_status:{self.a_open_mine}")

	def test_agent_status_errors_are_not_cached(self):
		from dialogbrain.client import DialogBrainError

		self._caps(agent_pause=True)
		frappe.cache().delete_value(f"dialogbrain_agent_status:{self.a_open_mine}")
		frappe.set_user(self.agent_a)
		self._agent_call(api.agent_status, self.a_open_mine, error=DialogBrainError("down"))
		client, _, out = self._agent_call(api.agent_status, self.a_open_mine, result={"paused": False})
		client.execute.assert_called_once()
		self.assertFalse(out["paused"])
		frappe.cache().delete_value(f"dialogbrain_agent_status:{self.a_open_mine}")

	def test_agent_status_without_thread_is_an_error_result(self):
		self._caps(agent_pause=True)
		frappe.db.set_value("DB Conversation", self.a_open_mine, {"thread_id": None, "thread_ref": None})
		frappe.set_user(self.agent_a)
		client, _, out = self._agent_call(api.agent_status, self.a_open_mine, result={"paused": True})
		client.execute.assert_not_called()
		self.assertTrue(out["supported"])
		self.assertTrue(out["error"])
