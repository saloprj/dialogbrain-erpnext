"""Whitelisted inbox read & action API (Plan 2, Task 1).

Every method that names a conversation calls :func:`assert_can_access` first. Lists apply
the same channel restriction as the registered ``permission_query_conditions``
(:func:`conversation_query`). DB Conversation fields other than ``status`` are permlevel 1,
so actions write them server-side with ``frappe.db.set_value`` after their own checks.
"""

import html as html_lib
import json
import re
from datetime import timedelta

import frappe
from bs4 import BeautifulSoup
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime, strip_html
from frappe.utils.html_utils import sanitize_html

from dialogbrain.permissions import (
	allowed_channels,
	assert_can_access,
	conversation_query,
	is_manager,
	permitted_users,
)
from dialogbrain.client import DialogBrainClient, DialogBrainError
from dialogbrain.party import PARTY_TYPES
from dialogbrain.sync.notify import publish

C = "`tabDB Conversation`"
SORT_KEY = f"COALESCE({C}.`last_message_at`, {C}.`creation`)"
PREVIEW_MAX = 120
MESSAGE_SEARCH_DAYS = 90
MESSAGE_SEARCH_MIN = 3
MAX_LIMIT = 500
DRAFT_WEBHOOK_LIMIT = 500
DRAFT_LIST_LIMIT = 300
# A reply the sender has claimed (pending -> unknown, no error yet) is still in flight
# for this long after the claim (file uploads up to 60 s + the send); the UI shows it
# as "sending". Older, or with an error text, it is a real "unknown".
IN_FLIGHT_SECONDS = 120
# Tags a message must never render: phishing forms, document-level/meta tags, embeds.
DISALLOWED_TAGS = [
	"form", "input", "button", "select", "textarea", "option", "link", "meta",
	"html", "head", "body", "iframe", "object", "embed", "style", "base",
]

ROW_FIELDS = (
	f"{C}.`name`",
	f"{C}.`title`",
	f"{C}.`contact_name`",
	f"{C}.`channel`",
	"ch.`channel_type` AS channel_type",
	"ch.`title` AS channel_title",
	f"{C}.`avatar_url`",
	f"{C}.`assigned_to`",
	"u.`full_name` AS assigned_to_name",
	f"{C}.`status`",
	f"{C}.`unread_count`",
	f"{C}.`needs_human`",
	f"{C}.`last_message_at`",
	f"{C}.`last_message_preview`",
	f"{C}.`last_message_direction`",
	f"{C}.`party_type`",
	f"{C}.`party`",
)
DETAIL_FIELDS = (
	f"{C}.`phone`",
	f"{C}.`email`",
	f"{C}.`handle`",
	f"{C}.`opened_at`",
	f"{C}.`closed_at`",
	f"{C}.`first_response_seconds`",
	f"{C}.`link_source`",
	f"{C}.`agent_override`",
	f"{C}.`contact`",
	"ch.`agent_mode` AS agent_mode",
)
FROM = (
	f"FROM {C} LEFT JOIN `tabDB Channel` ch ON ch.`name` = {C}.`channel` "
	f"LEFT JOIN `tabUser` u ON u.`name` = {C}.`assigned_to`"
)


# --- helpers ----------------------------------------------------------------


def _parse(value):
	"""dict/list, or its JSON string as posted by the browser; None for empty."""
	if value in (None, ""):
		return None
	if isinstance(value, str):
		try:
			return json.loads(value)
		except ValueError:
			frappe.throw(_("Invalid JSON argument"))
	return value


def _truthy(value):
	if isinstance(value, str):
		return value.strip().lower() in ("1", "true", "yes", "on")
	return bool(value)


def _limit(value, default=50):
	return min(max(cint(value) or default, 1), MAX_LIMIT)


def _escape_like(text):
	return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _require_read():
	if not frappe.has_permission("DB Conversation", "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


def _can_see_channel(channel, user):
	channels = allowed_channels(user)
	return channels is None or channel in channels


def sanitize_message_html(value):
	"""Message HTML safe to inject: Frappe's sanitiser minus forms/meta/embeds, no ``style``
	attributes at all (no full-screen ``position:fixed`` overlays), and every link opens in
	a new tab without access to the desk window (``target="_blank" rel="noopener noreferrer"``)."""
	clean = sanitize_html(value or "", always_sanitize=True, disallowed_tags=DISALLOWED_TAGS)
	if "style" not in clean and "<a" not in clean:
		return clean
	soup = BeautifulSoup(clean, "html.parser")
	for tag in soup.find_all(style=True):
		del tag["style"]
	for a in soup.find_all("a"):
		a["target"] = "_blank"
		a["rel"] = "noopener noreferrer"
	return str(soup)


def is_draft_truncated(text, full_text=False):
	"""True when DialogBrain cut the draft text: webhook ``draft.created`` sends 500 chars,
	``agents.list_drafts`` 300 chars + "...". ``full_text``: the complete text was fetched
	(``composer.get_full_draft``), so it is not cut whatever its length."""
	if full_text:
		return False
	text = text or ""
	return len(text) == DRAFT_WEBHOOK_LIMIT or (len(text) >= DRAFT_LIST_LIMIT and text.endswith("..."))


UNSUPPORTED_MESSAGE = "Your DialogBrain doesn't support this yet"


def has_capability(name):
	from dialogbrain.setup.agent_integration import has_capability as _has

	return _has(name)


def require_capability(name):
	"""Throw a clear error when the connected DialogBrain lacks ``name`` (Plan 3 Part A)."""
	if not has_capability(name):
		frappe.throw(_(UNSUPPORTED_MESSAGE))


def _plain(value):
	return html_lib.unescape(strip_html(value or "")).strip()


def _row(r):
	r = dict(r)
	r.pop("_sort_key", None)
	r["last_message_preview"] = strip_html(r.get("last_message_preview") or "")[:PREVIEW_MAX]
	return r


def _list_conditions(filters, user):
	"""SQL conditions (all parameterised) and their params for :func:`list_conversations`."""
	conditions, params = [], {}
	perm = conversation_query(user)
	if perm:
		conditions.append(perm)

	status = filters.get("status") or "Open"
	if status not in ("Open", "Closed", "All"):
		frappe.throw(_("Invalid status filter"))
	if status != "All":
		conditions.append(f"{C}.`status` = %(status)s")
		params["status"] = status

	scope = filters.get("scope") or "all"
	if scope == "mine":
		conditions.append(f"{C}.`assigned_to` = %(me)s")
		params["me"] = user
	elif scope == "unassigned":
		conditions.append(f"COALESCE({C}.`assigned_to`, '') = ''")
	elif scope != "all":
		frappe.throw(_("Invalid scope filter"))

	if _truthy(filters.get("needs_human")):
		conditions.append(f"{C}.`needs_human` = 1")

	if _truthy(filters.get("unanswered")):
		conditions.append(f"{C}.`last_message_direction` = 'in' AND {C}.`status` = 'Open'")

	channel = filters.get("channel")
	if channel:
		if not _can_see_channel(channel, user):
			frappe.throw(_("You do not have access to this channel"), frappe.PermissionError)
		conditions.append(f"{C}.`channel` = %(channel)s")
		params["channel"] = channel

	if filters.get("channel_type"):
		conditions.append("ch.`channel_type` = %(channel_type)s")
		params["channel_type"] = filters["channel_type"]

	names = filters.get("name")  # an empty list means no name restriction
	if names:
		# rows under the current filters, by name: realtime refresh of a batch of list rows
		if isinstance(names, str):
			names = [names]
		if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
			frappe.throw(_("Invalid name filter"))
		conditions.append(f"{C}.`name` IN %(conv_names)s")
		params["conv_names"] = tuple(names)

	party_type, party = filters.get("party_type"), filters.get("party")
	if party_type or party:
		# Customer / Lead form "Chats" section: both keys together, Customer or Lead only
		if not (party_type and party):
			frappe.throw(_("Filter by party needs both party_type and party"))
		if party_type not in PARTY_TYPES:
			frappe.throw(_("Party type must be Customer or Lead"))
		conditions.append(f"{C}.`party_type` = %(party_type)s AND {C}.`party` = %(party)s")
		params["party_type"] = party_type
		params["party"] = str(party)

	if filters.get("assigned_to") and is_manager(user):
		conditions.append(f"{C}.`assigned_to` = %(assigned_to)s")
		params["assigned_to"] = filters["assigned_to"]

	search = str(filters.get("search") or "").strip()
	if search:
		params["like"] = f"%{_escape_like(search)}%"
		ors = [f"{C}.`{f}` LIKE %(like)s" for f in ("title", "contact_name", "phone", "email", "handle")]
		if len(search) >= MESSAGE_SEARCH_MIN:
			params["since"] = now_datetime() - timedelta(days=MESSAGE_SEARCH_DAYS)
			ors.append(
				f"EXISTS (SELECT 1 FROM `tabDB Message` m WHERE m.`conversation` = {C}.`name` "
				"AND m.`sent_at` >= %(since)s AND m.`text` LIKE %(like)s)"
			)
		conditions.append("(" + " OR ".join(ors) + ")")
	return conditions, params


def _get_row(name, detail=False):
	fields = ROW_FIELDS + (DETAIL_FIELDS if detail else ())
	rows = frappe.db.sql(
		f"SELECT {', '.join(fields)} {FROM} WHERE {C}.`name` = %(name)s", {"name": name}, as_dict=True
	)
	return rows[0] if rows else None


def _changed(conversation, payload=None):
	publish(conversation, "conversation", payload or {})
	return _row(_get_row(conversation))


# --- list / counters ----------------------------------------------------------


@frappe.whitelist()
def list_conversations(filters=None, cursor=None, limit=50) -> dict:
	"""Conversation list, newest activity first, keyset-paged.

	Sort key ``COALESCE(last_message_at, creation)`` desc, then ``name`` desc; the cursor is
	``{"key", "name"}`` of the last row returned.
	"""
	_require_read()
	user = frappe.session.user
	filters = _parse(filters) or {}
	cursor = _parse(cursor)
	limit = _limit(limit)
	if not isinstance(filters, dict):
		frappe.throw(_("Invalid filters"))

	conditions, params = _list_conditions(filters, user)
	if cursor:
		if not isinstance(cursor, dict) or not cursor.get("key") or not cursor.get("name"):
			frappe.throw(_("Invalid cursor"))
		params["ck"] = get_datetime(cursor["key"])
		params["cn"] = str(cursor["name"])
		conditions.append(
			f"({SORT_KEY} < %(ck)s OR ({SORT_KEY} = %(ck)s AND {C}.`name` < %(cn)s))"
		)
	params["n"] = limit + 1
	where = " AND ".join(conditions) or "1=1"
	rows = frappe.db.sql(
		f"SELECT {', '.join(ROW_FIELDS)}, {SORT_KEY} AS _sort_key {FROM} WHERE {where} "
		f"ORDER BY {SORT_KEY} DESC, {C}.`name` DESC LIMIT %(n)s",
		params,
		as_dict=True,
	)
	next_cursor = None
	if len(rows) > limit:
		rows = rows[:limit]
		last = rows[-1]
		next_cursor = {"key": str(last["_sort_key"]), "name": last["name"]}
	return {"rows": [_row(r) for r in rows], "next_cursor": next_cursor}


@frappe.whitelist()
def counters() -> dict:
	"""Counts over the Open conversations the user can see."""
	_require_read()
	user = frappe.session.user
	conditions = [f"{C}.`status` = 'Open'"]
	perm = conversation_query(user)
	if perm:
		conditions.append(perm)
	row = frappe.db.sql(
		f"""SELECT
			COALESCE(SUM({C}.`unread_count`), 0),
			COALESCE(SUM(CASE WHEN {C}.`last_message_direction` = 'in' THEN 1 ELSE 0 END), 0),
			COALESCE(SUM(CASE WHEN {C}.`assigned_to` = %(me)s THEN 1 ELSE 0 END), 0),
			COALESCE(SUM(CASE WHEN COALESCE({C}.`assigned_to`, '') = '' THEN 1 ELSE 0 END), 0),
			COALESCE(SUM(CASE WHEN {C}.`needs_human` = 1 THEN 1 ELSE 0 END), 0)
		FROM {C} WHERE {' AND '.join(conditions)}""",
		{"me": user},
	)[0]
	keys = ("unread_total", "unanswered", "mine", "unassigned", "needs_human")
	return {k: cint(v) for k, v in zip(keys, row)}


@frappe.whitelist()
def channel_types() -> list:
	"""Distinct channel types among the conversations the user can see (the list's chips)."""
	_require_read()
	perm = conversation_query(frappe.session.user)
	where = f"WHERE {perm}" if perm else ""
	return frappe.db.sql_list(
		f"SELECT DISTINCT ch.`channel_type` FROM {C} JOIN `tabDB Channel` ch ON ch.`name` = {C}.`channel` "
		f"{where} ORDER BY ch.`channel_type`"
	)


# --- one conversation -----------------------------------------------------------


@frappe.whitelist()
def get_conversation(name: str) -> dict:
	assert_can_access(name)
	r = _get_row(name, detail=True)
	data = _row(r)
	data["channel"] = {
		"name": r["channel"],
		"title": data.pop("channel_title"),
		"channel_type": data.pop("channel_type"),
		"agent_mode": data.pop("agent_mode"),
	}
	data["permissions"] = {
		"can_assign_any": is_manager(),
		# what the connected DialogBrain supports (Plan 3): the UI shows the actions only then
		"agent_pause": has_capability("agent_pause"),
		"full_drafts": has_capability("full_drafts"),
	}
	return data


def _message_rows(where, params, order="DESC", limit=None):
	key = "COALESCE(m.`sent_at`, m.`creation`)"
	sql = (
		f"SELECT m.`name`, m.`client_ref`, m.`direction`, m.`author_type`, m.`sent_by`, "
		f"u.`full_name` AS sent_by_name, m.`text`, m.`sent_at`, m.`delivery_status`, m.`error`, "
		f"m.`creation`, m.`modified`, m.`db_id`, "
		f"CASE WHEN m.`delivery_status` = 'unknown' THEN m.`raw` END AS _raw, {key} AS _key "
		f"FROM `tabDB Message` m LEFT JOIN `tabUser` u ON u.`name` = m.`sent_by` "
		f"WHERE {where} ORDER BY {key} {order}, m.`creation` {order}, m.`name` {order}"
	)
	if limit:
		sql += " LIMIT %(n)s"
		params["n"] = limit
	return frappe.db.sql(sql, params, as_dict=True)


def _format_messages(rows):
	from dialogbrain.sync.sender import unknown_retryable

	names = tuple(r["name"] for r in rows)
	attachments = {}
	if names:
		for a in frappe.db.sql(
			"""SELECT `parent`, `file_id`, `filename`, `mime_type`, `media_type`, `size`
			FROM `tabDB Message Attachment`
			WHERE `parenttype` = 'DB Message' AND `parent` IN %(names)s ORDER BY `parent`, `idx`""",
			{"names": names},
			as_dict=True,
		):
			parent = a.pop("parent")
			attachments.setdefault(parent, []).append(dict(a))
	in_flight_since = now_datetime() - timedelta(seconds=IN_FLIGHT_SECONDS)
	out = []
	for r in rows:
		raw = r["text"] or ""
		out.append(
			{
				"name": r["name"],
				"client_ref": r["client_ref"],
				"direction": r["direction"],
				"author_type": r["author_type"],
				"sent_by": r["sent_by"],
				"sent_by_name": r["sent_by_name"],
				"html": sanitize_message_html(raw),
				"text": _plain(raw),
				"sent_at": r["sent_at"],
				"delivery_status": r["delivery_status"],
				"error": r["error"] if r["delivery_status"] == "failed" else None,
				"in_flight": bool(
					r["delivery_status"] == "unknown"
					and not r["error"]
					and r["modified"]
					and get_datetime(r["modified"]) >= in_flight_since
				),
				"attachments": attachments.get(r["name"], []),
				# failed, or unknown and safe to send again with its request_id (Plan 3 B4)
				"can_retry": r["direction"] == "out" and not r["db_id"] and (
					r["delivery_status"] == "failed"
					or unknown_retryable(r["delivery_status"], r["db_id"], r["_raw"])
				),
			}
		)
	return out


@frappe.whitelist()
def get_messages(conversation: str, before=None, limit=50) -> dict:
	"""Messages newest first. Order: ``sent_at`` (``creation`` when unset), then ``creation``,
	then ``name``; ``next_before`` is ``{"sent_at", "creation", "name"}`` of the last row."""
	assert_can_access(conversation)
	before = _parse(before)
	limit = _limit(limit)
	params = {"conversation": conversation}
	where = "m.`conversation` = %(conversation)s"
	if before:
		if not isinstance(before, dict) or not before.get("sent_at") or not before.get("name"):
			frappe.throw(_("Invalid cursor"))
		key = "COALESCE(m.`sent_at`, m.`creation`)"
		params.update(
			{
				"bk": get_datetime(before["sent_at"]),
				"bc": get_datetime(before.get("creation") or before["sent_at"]),
				"bn": str(before["name"]),
			}
		)
		where += (
			f" AND ({key} < %(bk)s OR ({key} = %(bk)s AND (m.`creation` < %(bc)s "
			"OR (m.`creation` = %(bc)s AND m.`name` < %(bn)s))))"
		)
	rows = _message_rows(where, params, limit=limit + 1)
	next_before = None
	if len(rows) > limit:
		rows = rows[:limit]
		last = rows[-1]
		next_before = {"sent_at": str(last["_key"]), "creation": str(last["creation"]), "name": last["name"]}
	return {"rows": _format_messages(rows), "next_before": next_before}


@frappe.whitelist()
def get_messages_by_name(conversation: str, names=None) -> list:
	"""Rows for ``names`` (realtime refresh): the ones that exist in ``conversation``.
	Missing names are skipped; a name of another conversation's message is refused."""
	assert_can_access(conversation)
	names = _parse(names) or []
	if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
		frappe.throw(_("Invalid message names"))
	if not names:
		return []
	wanted = tuple(set(names))
	# A name may be gone by now (e.g. an echo folded into its reply): it is skipped. A name
	# that exists in another conversation is refused.
	foreign = frappe.db.sql(
		"SELECT 1 FROM `tabDB Message` WHERE `name` IN %(names)s AND `conversation` != %(conversation)s LIMIT 1",
		{"conversation": conversation, "names": wanted},
	)
	if foreign:
		frappe.throw(_("Message does not belong to this conversation"), frappe.PermissionError)
	rows = _message_rows(
		"m.`conversation` = %(conversation)s AND m.`name` IN %(names)s",
		{"conversation": conversation, "names": wanted},
	)
	return _format_messages(rows)


@frappe.whitelist()
def get_drafts(conversation: str) -> list:
	"""Pending AI drafts, newest first. ``truncated``: DialogBrain cut the text (webhook
	``draft.created`` at 500 chars; ``agents.list_drafts`` at 300 + "...") and the full text
	has not been fetched yet."""
	assert_can_access(conversation)
	rows = frappe.db.sql(
		"""SELECT `name`, `draft_id`, `text`, `created_at`, `full_text` FROM `tabDB Draft`
		WHERE `conversation` = %(conversation)s AND `status` = 'pending'
		ORDER BY COALESCE(`created_at`, `creation`) DESC, `creation` DESC, `name` DESC""",
		{"conversation": conversation},
		as_dict=True,
	)
	for r in rows:
		r["truncated"] = is_draft_truncated(r["text"], r.pop("full_text"))
	return [dict(r) for r in rows]


# --- actions -------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def mark_read(conversation: str) -> dict:
	"""Clear ``unread_count``; writes and publishes only when there was something unread."""
	assert_can_access(conversation)
	if not cint(frappe.db.get_value("DB Conversation", conversation, "unread_count")):
		return _row(_get_row(conversation))
	frappe.db.set_value("DB Conversation", conversation, "unread_count", 0)
	return _changed(conversation, {"unread_count": 0})


@frappe.whitelist(methods=["POST"])
def assign(conversation: str, user: "str | None" = None) -> dict:
	"""Assign (or with ``user`` empty, unassign).

	Chat Agent: only to themself, and only when unassigned or already theirs; may unassign
	only their own. Chat Manager / System Manager: anyone, or nobody. The target must be a
	user permitted on the conversation's channel.

	Race-safe: the row is locked for the request transaction, and an agent's write is a
	conditional UPDATE re-checked afterwards, so of two agents taking the same unassigned
	conversation at once only one succeeds; the other gets ``PermissionError``.
	"""
	assert_can_access(conversation)
	me = frappe.session.user
	user = user or None
	manager = is_manager(me)
	current = frappe.db.get_value(
		"DB Conversation", conversation, ["assigned_to", "channel"], as_dict=True, for_update=True
	)
	if not manager:
		if user and user != me:
			frappe.throw(_("You can only assign conversations to yourself"), frappe.PermissionError)
		if user is None and current.assigned_to != me:
			frappe.throw(_("You can only unassign your own conversations"), frappe.PermissionError)
		if user and current.assigned_to and current.assigned_to != me:
			frappe.throw(_("This conversation is assigned to someone else"), frappe.PermissionError)
	if user and user not in permitted_users(current.channel):
		frappe.throw(_("{0} cannot be assigned to this conversation").format(user))
	if manager:
		if (current.assigned_to or None) != user:
			frappe.db.set_value("DB Conversation", conversation, "assigned_to", user)
	else:
		_agent_assign(conversation, me, user)
	return _changed(conversation, {"assigned_to": user})


def _agent_assign(conversation, me, user):
	"""Conditional write for an agent: take only if unassigned or already theirs; unassign
	only if theirs. Re-read under the row lock the UPDATE holds to detect a lost race."""
	allowed = "('', %(me)s)" if user else "(%(me)s)"
	frappe.db.sql(
		f"""UPDATE {C} SET `assigned_to` = %(user)s, `modified` = %(now)s, `modified_by` = %(me)s
		WHERE `name` = %(name)s AND COALESCE(`assigned_to`, '') IN {allowed}""",
		{"user": user, "me": me, "now": now_datetime(), "name": conversation},
	)
	after = frappe.db.sql(
		f"SELECT `assigned_to` FROM {C} WHERE `name` = %(name)s FOR UPDATE", {"name": conversation}
	)[0][0]
	if (after or None) != user:
		frappe.throw(_("This conversation is assigned to someone else"), frappe.PermissionError)


@frappe.whitelist(methods=["POST"])
def set_status(conversation: str, status: str) -> dict:
	"""Close (``closed_at`` = now, ``needs_human`` cleared) or reopen (``opened_at`` = now,
	``closed_at`` cleared, ``first_response_seconds`` reset to 0)."""
	assert_can_access(conversation)
	if status not in ("Open", "Closed"):
		frappe.throw(_("Invalid status"))
	current = frappe.db.get_value("DB Conversation", conversation, "status")
	if current == status:
		return _row(_get_row(conversation))
	if status == "Closed":
		updates = {"status": "Closed", "closed_at": now_datetime(), "needs_human": 0}
	else:
		# 0 = first response "not measured yet", as in the Plan 1 reopen on inbound
		updates = {
			"status": "Open",
			"opened_at": now_datetime(),
			"closed_at": None,
			"first_response_seconds": 0,
		}
	frappe.db.set_value("DB Conversation", conversation, updates)
	payload = {"status": status}
	if status == "Closed":
		payload["needs_human"] = 0
	return _changed(conversation, payload)


@frappe.whitelist()
def assignable_users(conversation: "str | None" = None) -> list:
	"""Users the current user may pick in the Assign menu (or, without ``conversation``, in
	the list's manager filter): ``[{"name", "full_name"}]``.

	Manager: everyone permitted on the conversation's channel (:func:`permitted_users`, the
	same set :func:`assign` accepts); without a conversation, every enabled Chat Manager /
	Chat Agent. Chat Agent: only themself, and only when permitted on the channel (they may
	assign only to themself); without a conversation, nobody (the filter is manager-only).
	"""
	me = frappe.session.user
	if conversation:
		assert_can_access(conversation)
		channel = frappe.db.get_value("DB Conversation", conversation, "channel")
		users = permitted_users(channel)
		if not is_manager(me):
			users = [u for u in users if u == me]
	else:
		_require_read()
		if not is_manager(me):
			return []
		users = frappe.db.sql_list(
			"""SELECT DISTINCT u.`name` FROM `tabUser` u
			JOIN `tabHas Role` hr ON hr.`parent` = u.`name` AND hr.`parenttype` = 'User'
			WHERE u.`enabled` = 1 AND hr.`role` IN ('Chat Manager', 'Chat Agent')"""
		)
	if not users:
		return []
	names = frappe.db.sql(
		"SELECT `name`, `full_name` FROM `tabUser` WHERE `name` IN %(users)s",
		{"users": tuple(users)},
		as_dict=True,
	)
	full = {r.name: r.full_name for r in names}
	return sorted(
		({"name": u, "full_name": full.get(u) or u} for u in users),
		key=lambda r: (r["full_name"] or "").lower(),
	)


# --- take over / give back (Plan 3 B4) ---------------------------------------------
#
# DialogBrain tools (verified in the Part A worktree, ``threads/agent_pause.py``):
# ``threads.set_agent_paused(thread_id, paused)`` -> ``{thread_id, paused, paused_at}``
# (operator only: an API key qualifies; needs EDIT in the workspace) and
# ``threads.get_agent_status(thread_id)`` -> ``{thread_id, paused, paused_by, paused_at}``,
# ``paused_by`` "operator" for a person's pause / take-over, null for the automatic pause a
# manual reply sets. ``thread_id`` takes the internal id or the channel ref.

_SECRET = re.compile(r"db_(?:live|test)_\w+")
AGENT_ERROR_MAX = 200


def _thread_param(conversation):
	conv = frappe.db.get_value("DB Conversation", conversation, ["thread_id", "thread_ref"], as_dict=True)
	thread = conv and (conv.thread_id or conv.thread_ref)
	if not thread:
		frappe.throw(_("The conversation has no DialogBrain thread id"))
	return thread


def _agent_reason(exc):
	return _SECRET.sub("[hidden]", str(exc) or type(exc).__name__)[:AGENT_ERROR_MAX]


def _set_agent_paused(conversation, paused):
	assert_can_access(conversation)
	require_capability("agent_pause")
	params = {"thread_id": _thread_param(conversation), "paused": paused}
	try:
		data = DialogBrainClient.from_settings().execute("threads.set_agent_paused", params) or {}
	except DialogBrainError as exc:
		frappe.log_error(title="DialogBrain threads.set_agent_paused failed", message=frappe.get_traceback())
		frappe.throw(_("DialogBrain could not change the agent on this conversation: {0}").format(_agent_reason(exc)))
	override = "human" if paused else "agent"
	frappe.db.set_value("DB Conversation", conversation, "agent_override", override)
	row = _changed(conversation, {"agent_override": override})
	row["agent_override"] = override
	row["agent"] = {
		"paused": bool(data.get("paused", paused)),
		"paused_by": "operator" if paused else None,
		"paused_at": data.get("paused_at"),
	}
	return row


@frappe.whitelist(methods=["POST"])
def take_over(conversation: str) -> dict:
	"""The person takes the conversation from the AI agent (DialogBrain pauses the agent in
	this thread; no resume policy lifts it). Same access rule as :func:`set_status`."""
	return _set_agent_paused(conversation, True)


@frappe.whitelist(methods=["POST"])
def give_back(conversation: str) -> dict:
	"""Hand the conversation back to the AI agent (any pause, operator or automatic)."""
	return _set_agent_paused(conversation, False)


@frappe.whitelist()
def agent_status(conversation: str) -> dict:
	"""``{supported, paused, paused_by, paused_at}``; ``{supported: False}`` without the
	capability; ``error`` (and nothing else) when DialogBrain could not answer."""
	assert_can_access(conversation)
	if not has_capability("agent_pause"):
		return {"supported": False}
	try:
		data = DialogBrainClient.from_settings().execute(
			"threads.get_agent_status", {"thread_id": _thread_param(conversation)}
		) or {}
	except DialogBrainError as exc:
		return {"supported": True, "error": _agent_reason(exc)}
	return {
		"supported": True,
		"paused": bool(data.get("paused")),
		"paused_by": data.get("paused_by"),
		"paused_at": data.get("paused_at"),
	}
