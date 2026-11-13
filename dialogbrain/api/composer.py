"""Whitelisted composer API: notes, quick replies and draft actions (Plan 2, Task 2).

Every method that names a conversation, note or draft calls ``assert_can_access`` on the
owning conversation first.

DialogBrain tool params (verified against
``apps/api-gateway/src/tools/definitions/agents/{approve_draft,reject_draft}.py``):

* ``agents.approve_draft``: ``draft_id`` (integer, required), ``edited_text`` (string,
  optional). Data: ``status`` is ``sent`` | ``approved_not_sent`` (+ ``send_error``) |
  ``approved`` | ``expired``; ``success=False`` when the draft is unknown or already
  processed. The tool sends through ``messages.send``; the outgoing message reaches this
  app through the normal webhook/echo path, so nothing is inserted here.
* ``agents.reject_draft``: ``draft_id`` (integer, required), ``reason`` (string, optional).

* ``agents.get_draft``: ``draft_id`` (integer, required) -> ``{draft_id, thread_id, status,
  response_text, trigger_text, created_at}`` with the FULL text (Plan 3 Part A; capability
  ``full_drafts``).

Draft text limitation: Plan 1 stores at most 500 chars of a draft (webhook) and
``agents.list_drafts`` cuts at 300 + "...". A draft whose text looks cut
(:func:`dialogbrain.api.conversations.is_draft_truncated`) can only be approved as is; an
edited approve is refused, as it would send the truncated text. With ``full_drafts``,
:func:`get_full_draft` stores the complete text (``full_text`` = 1) first, and the draft can
then be edited.
"""

import re

import frappe
from frappe import _

from dialogbrain.api.conversations import is_draft_truncated, require_capability
from dialogbrain.client import DialogBrainClient, DialogBrainError, DialogBrainTimeout
from dialogbrain.permissions import assert_can_access, is_manager
from dialogbrain.sync.notify import publish

TOOL_ERROR_MAX = 200


def _require(doctype):
	if not frappe.has_permission(doctype, "read"):
		frappe.throw(_("Not permitted"), frappe.PermissionError)


# --- notes ---------------------------------------------------------------------


@frappe.whitelist()
def list_notes(conversation: str) -> list:
	assert_can_access(conversation)
	return frappe.db.sql(
		"""SELECT n.`name`, n.`text`, n.`owner`, u.`full_name` AS owner_name, n.`creation`
		FROM `tabDB Note` n LEFT JOIN `tabUser` u ON u.`name` = n.`owner`
		WHERE n.`conversation` = %(conversation)s ORDER BY n.`creation`, n.`name`""",
		{"conversation": conversation},
		as_dict=True,
	)


@frappe.whitelist(methods=["POST"])
def add_note(conversation: str, text: str) -> dict:
	assert_can_access(conversation)
	text = (text or "").strip()
	if not text:
		frappe.throw(_("A note cannot be empty"))
	doc = frappe.get_doc({"doctype": "DB Note", "conversation": conversation, "text": text}).insert()
	publish(conversation, "conversation", {"notes": True})
	return {"name": doc.name, "text": doc.text, "owner": doc.owner, "creation": doc.creation}


@frappe.whitelist(methods=["POST"])
def delete_note(note: str) -> dict:
	row = frappe.db.get_value("DB Note", note, ["conversation", "owner"], as_dict=True)
	if not row:
		frappe.throw(_("Note not found"), frappe.DoesNotExistError)
	assert_can_access(row.conversation)
	if row.owner != frappe.session.user and not is_manager():
		frappe.throw(_("Only the author or a manager can delete a note"), frappe.PermissionError)
	frappe.delete_doc("DB Note", note, ignore_permissions=True)
	publish(row.conversation, "conversation", {"notes": True})
	return {"name": note}


# --- quick replies ---------------------------------------------------------------


def _party_name(conv):
	if not (conv.party_type and conv.party):
		return None
	meta = frappe.get_meta(conv.party_type)
	field = meta.title_field or "name"
	return frappe.db.get_value(conv.party_type, conv.party, field)


def _render(text, values):
	for key, value in values.items():
		text = text.replace("{" + key + "}", value or "")
	return text


@frappe.whitelist()
def list_quick_replies(conversation: str) -> list:
	"""The user's own plus shared quick replies whose company is empty or the channel's."""
	assert_can_access(conversation)
	_require("DB Quick Reply")
	user = frappe.session.user
	conv = frappe.db.get_value(
		"DB Conversation", conversation, ["channel", "contact_name", "party_type", "party"], as_dict=True
	)
	company = frappe.db.get_value("DB Channel", conv.channel, "company")
	rows = frappe.db.sql(
		"""SELECT `name`, `title`, `shortcut`, `text` FROM `tabDB Quick Reply`
		WHERE (`owner` = %(user)s OR `shared` = 1)
		AND (COALESCE(`company`, '') = '' OR `company` = %(company)s)
		ORDER BY `title`, `name`""",
		{"user": user, "company": company or ""},
		as_dict=True,
	)
	values = {
		"customer_name": _party_name(conv) or conv.contact_name,
		"manager_name": frappe.db.get_value("User", user, "full_name"),
		"company": company,
	}
	for r in rows:
		r["text"] = _render(r["text"] or "", values)
	return rows


# --- drafts ------------------------------------------------------------------------


def _pending_draft(draft):
	row = frappe.db.get_value(
		"DB Draft", draft, ["name", "draft_id", "conversation", "text", "status", "full_text"], as_dict=True
	)
	if not row or not row.conversation:
		frappe.throw(_("Draft not found"), frappe.DoesNotExistError)
	assert_can_access(row.conversation)
	if row.status != "pending":
		frappe.throw(_("This draft was already handled"))
	return row


def _draft_param(row):
	"""DialogBrain's ``draft_id`` is an integer; ours is stored as Data."""
	try:
		return int(row.draft_id)
	except (TypeError, ValueError):
		frappe.throw(_("This draft has an invalid DialogBrain id"))


GONE_MESSAGE = "This draft was already handled or expired in DialogBrain."
UNKNOWN_MESSAGE = "Outcome unknown — check the conversation before trying again."
_SECRET = re.compile(r"db_(?:live|test)_\w+")


class _Gone(Exception):
	"""DialogBrain no longer holds the draft as pending (expired / not found / processed)."""


class _Unknown(Exception):
	"""DialogBrain timed out: the call may or may not have taken effect."""


def _is_gone(e):
	code = str(e.code or "").lower()
	return code in ("not_found", "not-found") or e.status == 404 or "not found" in str(e).lower() \
		or "already processed" in str(e).lower()


def _safe(text):
	return _SECRET.sub("[hidden]", str(text or ""))[:TOOL_ERROR_MAX]


def _call(tool_id, params):
	"""Run a DialogBrain tool. Terminal outcomes raise :class:`_Gone` / :class:`_Unknown`
	(handled by the caller as results, so the status change commits); any other DialogBrain
	failure becomes a safe message (no key, no upstream detail) and the draft stays pending."""
	try:
		return DialogBrainClient.from_settings().execute(tool_id, params) or {}
	except DialogBrainTimeout:
		raise _Unknown() from None
	except DialogBrainError as e:
		if _is_gone(e):
			raise _Gone() from None
		frappe.log_error(title=f"DialogBrain {tool_id} failed", message=frappe.get_traceback())
		frappe.throw(_("DialogBrain could not process this draft. Try again later."))


def _set_status(row, status):
	frappe.db.set_value("DB Draft", row.name, "status", status)
	publish(row.conversation, "draft", {"draft": row.name, "status": status})


def _result(row, status, sent=False, message=""):
	return {"name": row.name, "status": status, "sent": sent, "message": message}


def _gone(row):
	_set_status(row, "superseded")
	return _result(row, "superseded", message=_(GONE_MESSAGE))


@frappe.whitelist(methods=["POST"])
def approve_draft(draft: str, text: "str | None" = None) -> dict:
	"""Returns ``{"status": approved|superseded|unknown, "sent", "message"}``. Terminal
	DialogBrain outcomes are results, not exceptions, so the local status change commits."""
	row = _pending_draft(draft)
	params = {"draft_id": _draft_param(row)}
	if text is not None and not text.strip():
		frappe.throw(_("Draft text is empty."))
	if text is not None and text != (row.text or ""):
		if is_draft_truncated(row.text, row.full_text):
			frappe.throw(
				_("This draft was shortened by DialogBrain. Approve it as is or reject it.")
			)  # with full_drafts the UI fetches the full text (get_full_draft) before Edit
		params["edited_text"] = text
	try:
		data = _call("agents.approve_draft", params)
	except _Unknown:
		return _result(row, "unknown", message=_(UNKNOWN_MESSAGE))
	except _Gone:
		return _gone(row)
	status = data.get("status")
	if status == "expired":
		return _gone(row)
	_set_status(row, "approved")
	if status == "approved_not_sent":
		return _result(row, "approved", message=_safe(data.get("send_error")) or _("Approved but not sent."))
	return _result(row, "approved", sent=status == "sent")


@frappe.whitelist(methods=["POST"])
def reject_draft(draft: str) -> dict:
	row = _pending_draft(draft)
	try:
		_call("agents.reject_draft", {"draft_id": _draft_param(row)})
	except _Unknown:
		return _result(row, "unknown", message=_(UNKNOWN_MESSAGE))
	except _Gone:
		return _gone(row)
	_set_status(row, "rejected")
	return _result(row, "rejected")


@frappe.whitelist(methods=["POST"])
def get_full_draft(draft: str) -> dict:
	"""Fetch the draft's complete text from DialogBrain (``agents.get_draft``) and store it
	in place of the cut one, so the draft can be edited.

	Returns ``{"name", "status": "pending", "text", "truncated": False}``, or the
	``superseded`` result when DialogBrain no longer holds the draft as pending."""
	row = _pending_draft(draft)
	require_capability("full_drafts")
	if row.full_text:
		return {"name": row.name, "status": "pending", "text": row.text or "", "truncated": False}
	try:
		data = DialogBrainClient.from_settings().execute("agents.get_draft", {"draft_id": _draft_param(row)}) or {}
	except DialogBrainError as e:
		if not isinstance(e, DialogBrainTimeout) and _is_gone(e):
			return _gone(row)
		frappe.log_error(title="DialogBrain agents.get_draft failed", message=frappe.get_traceback())
		frappe.throw(_("Could not load the full draft text from DialogBrain. Try again later."))
	if (data.get("status") or "pending") != "pending":
		return _gone(row)
	# Defence in depth (the draft id is workspace-scoped and comes from our own row): the
	# check is skipped when the conversation knows only its thread_ref (no internal id to
	# compare) or DialogBrain returns no thread_id.
	thread = frappe.db.get_value("DB Conversation", row.conversation, "thread_id")
	if thread and data.get("thread_id") is not None and str(data.get("thread_id")) != str(thread):
		frappe.log_error(
			title="DialogBrain agents.get_draft: thread mismatch",
			message=f"{row.name}: draft {row.draft_id} belongs to thread {data.get('thread_id')}, not {thread}",
		)
		frappe.throw(_("DialogBrain returned a draft of another conversation."))
	text = data.get("response_text")
	if not isinstance(text, str) or not text.strip():
		frappe.throw(_("DialogBrain returned no text for this draft."))
	frappe.db.set_value("DB Draft", row.name, {"text": text, "full_text": 1})
	publish(row.conversation, "draft", {"draft": row.name})
	return {"name": row.name, "status": "pending", "text": text, "truncated": False}
