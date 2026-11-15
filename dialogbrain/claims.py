"""Who owns a number that is being connected from ERPNext.

``api/connect_channel.start`` stores a **DB Channel Claim**: the DialogBrain connect
session, who started it and who the channel is for. The claim is what gives the new
DB Channel its ``allowed_users``, and it does so from three places, so that the owner is
set even when the browser tab is closed right after the scan:

* the browser poll (``api/connect_channel.get``) on ``connected``;
* ``sweep``, a background job the every-minute tick starts while claims are open;
* ``on_new_channel``, called by ``upsert_channel`` when a webhook or the backfill is about
  to create a channel while a claim of that type is open.

A DB Channel with no allowed users is visible to every Chat Agent. A channel that might be
the number of an open claim is therefore never created like that. When it cannot be tied
to its claim on the spot it is created **restricted** ("Managers only": no agent sees it,
not even the owners of the other open claims) and listed on the open claims as
provisional. It stops being restricted only when

* a claim turns out to be that number (``resolve``: it goes to the claim's owner), or
* DialogBrain has told us, for every claim that listed it, that the claim's session did
  not connect (the number was connected elsewhere: it is open, as before this feature).

A claim that ends without such an answer (DialogBrain unreachable, a start that timed
out) keeps its channels restricted; a manager decides on the DB Channel form.

No phone number, code or password passes through here. No DialogBrain call is made from
the webhook path.
"""

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from dialogbrain import compat
from dialogbrain.client import DialogBrainClient, DialogBrainError, DialogBrainTimeout
from dialogbrain.connect_gate import GET

CLAIM = "DB Channel Claim"
CHANNEL = "DB Channel"
SETTINGS = "DialogBrain Settings"
GRACE_SECONDS = 600  # after the session's expiry a connected session still answers
GIVE_UP_SECONDS = 24 * 3600  # an unanswerable claim is closed (its channels stay restricted)
SWEEP_TIMEOUT = 5
SWEEP_LIMIT = 30
SWEEP_MAX_ERRORS = 2
SWEEP_JOB = "dialogbrain.claims.sweep"
SWEEP_JOB_ID = "dialogbrain_claims_sweep"
TAKEN = "This number is already assigned to another employee"
SHARED = "This number is already connected as a shared channel. A manager can assign it to you"
_FIELDS = [
	"name",
	"session_id",
	"requested_by",
	"owner_user",
	"channel_type",
	"status",
	"account_id",
	"expires_at",
	"provisional_channels",
]


def _s(value) -> str:
	return str(value).strip() if value not in (None, "") else ""


def _type(value) -> str:
	return _s(value).lower()


# --- channel users ----------------------------------------------------------


def _allowed(channel) -> list:
	return frappe.get_all(
		"DB Channel User",
		filters={"parent": channel, "parenttype": CHANNEL},
		pluck="user",
		order_by="idx",
	)


def _set_allowed(channel, users, restricted=0) -> None:
	doc = frappe.get_doc(CHANNEL, channel)
	doc.set("allowed_users", [{"user": user} for user in dict.fromkeys(u for u in users if u)])
	doc.restricted = restricted
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)


def _provisional(claim) -> list:
	return [c for c in (claim.get("provisional_channels") or "").split("\n") if c]


def _listed_elsewhere(channel, claim_name) -> bool:
	"""Another claim (open, or ended without an answer) still lists ``channel``."""
	rows = frappe.get_all(
		CLAIM,
		filters={"provisional_channels": ["like", f"%{channel}%"], "name": ["!=", claim_name]},
		fields=["name", "provisional_channels"],
	)
	return any(channel in _provisional(row) for row in rows)


def _release_provisional(claim, keep=None, confirmed=False) -> None:
	"""The claim is settled. ``confirmed``: DialogBrain said what became of its session, so
	the channels restricted "in case they are this claim's" (other than ``keep``, the one
	it turned out to be) are not its number. Without that answer they stay restricted and
	stay listed on the claim."""
	if not confirmed:
		return
	for channel in _provisional(claim):
		if channel == keep or not frappe.db.exists(CHANNEL, channel):
			continue
		if _listed_elsewhere(channel, claim.name):
			continue
		if frappe.db.get_value(CHANNEL, channel, "restricted"):
			frappe.db.set_value(CHANNEL, channel, "restricted", 0)
			announce(channel)
	if claim.get("provisional_channels"):
		frappe.db.set_value(CLAIM, claim.name, "provisional_channels", None, update_modified=False)


def _close(claim, status, account_id=None, keep=None, confirmed=False) -> None:
	values = {"status": status, "resolved_at": now_datetime()}
	if account_id:
		values["account_id"] = account_id
	frappe.db.set_value(CLAIM, claim.name, values)
	_release_provisional(claim, keep=keep, confirmed=confirmed)


def close(claim_name, status, confirmed=False) -> None:
	"""Mark an open claim ``expired`` / ``cancelled`` / ``failed``.

	``confirmed=True`` only when DialogBrain itself answered that the session is not
	connected (see ``_release_provisional``)."""
	claim = frappe.db.get_value(CLAIM, claim_name, _FIELDS, as_dict=True, for_update=True)
	if claim and claim.status == "open":
		_close(claim, status, confirmed=confirmed)


# --- resolve ----------------------------------------------------------------


def announce(channel) -> None:
	"""Tell the inboxes of the people who may see ``channel`` that it exists now; its agent
	mode is read in the background (the hourly job would get to it anyway)."""
	from dialogbrain.sync.notify import REALTIME_PREFIX, realtime_users

	for user in realtime_users(channel):
		frappe.publish_realtime(
			REALTIME_PREFIX + "channel_connected", {"channel": channel}, user=user, after_commit=True
		)
	try:
		frappe.enqueue(
			"dialogbrain.setup.agent_modes.sync",
			queue="short",
			enqueue_after_commit=True,
			job_id="dialogbrain_agent_modes_after_connect",
			deduplicate=True,
		)
	except Exception:
		pass


def resolve(claim_name, state: dict) -> dict:
	"""The claim's session is connected as ``state["account_id"]``: make sure the DB Channel
	exists and belongs to the claim's owner. Safe to call twice and from any user.

	Returns ``{"channel", "assigned", "message"}``; ``assigned`` is False (with a message
	that names nobody) when the number is already someone else's, or already shared."""
	from dialogbrain.sync.upsert import upsert_channel

	claim = frappe.db.get_value(CLAIM, claim_name, _FIELDS, as_dict=True, for_update=True)
	if not claim:
		return {"channel": None, "assigned": False, "message": None}
	account_id = _s((state or {}).get("account_id")) or _s(claim.account_id)
	if not account_id:
		return {"channel": None, "assigned": False, "message": None}
	owner = claim.owner_user
	first = claim.status != "connected"
	existed = bool(frappe.db.exists(CHANNEL, account_id))
	title = _s((state or {}).get("name")) or _s((state or {}).get("phone")) or None
	channel = upsert_channel(account_id, claim.channel_type, title, check_claims=False)
	users = _allowed(channel)
	restricted = bool(frappe.db.get_value(CHANNEL, channel, "restricted"))

	assigned, message = True, None
	if not existed or restricted:
		# new here, or waiting for exactly this: it is the claim's number
		wanted = [owner] if owner else []
		if users != wanted or restricted:
			_set_allowed(channel, wanted)
	elif owner and owner in users:
		pass
	elif not users:
		if owner:
			# an open, shared channel is not taken away from the other agents by a re-scan
			assigned, message = False, _(SHARED)
	else:
		assigned, message = False, _(TAKEN)

	if first:
		_close(claim, "connected", account_id=account_id, keep=channel, confirmed=True)
		announce(channel)
	return {"channel": channel, "assigned": assigned, "message": message}


# --- a channel appears before its claim is resolved -------------------------


def _open(channel_type=None, limit=None) -> list:
	filters = {"status": "open"}
	if channel_type:
		filters["channel_type"] = channel_type
	return frappe.get_all(CLAIM, filters=filters, fields=_FIELDS, order_by="expires_at asc", limit=limit)


def _age_past_expiry(claim, now=None) -> float:
	if not claim.expires_at:
		return 0
	return ((now or now_datetime()) - get_datetime(claim.expires_at)).total_seconds()


def on_new_channel(account_id, channel_type, external_ref=None) -> dict:
	"""How to create a DB Channel that does not exist yet:
	``{"users": [...], "restricted": 0 | 1}``. No network call: this runs inside webhook
	processing. ``restricted`` when it may be the number of an open claim and DialogBrain
	did not say which (no ``external_ref``); the sweep, started right away, settles it."""
	opened = {"users": [], "restricted": 0}
	channel_type = _type(channel_type)
	if not channel_type or not frappe.db.exists(CLAIM, {"status": "open", "channel_type": channel_type}):
		return opened
	account_id = _s(account_id)
	candidates = _open(channel_type)
	match = next((c for c in candidates if external_ref and c.name == _s(external_ref)), None)
	if match:
		# upsert_channel inserts the row right after this (and announces it)
		_close(match, "connected", account_id=account_id, keep=account_id, confirmed=True)
		return {"users": [match.owner_user] if match.owner_user else [], "restricted": 0, "announce": True}
	if not any(c.owner_user for c in candidates):
		return opened  # only shared numbers are being connected: open is what they asked for
	for claim in candidates:
		channels = [*_provisional(claim), account_id]
		frappe.db.set_value(
			CLAIM, claim.name, "provisional_channels", "\n".join(dict.fromkeys(channels)), update_modified=False
		)
	enqueue_sweep()
	return {"users": [], "restricted": 1}


# --- sweep ------------------------------------------------------------------


def enqueue_sweep() -> None:
	"""Run ``sweep`` in the background once the current transaction is committed."""
	try:
		frappe.enqueue(
			SWEEP_JOB, queue="short", timeout=300, job_id=SWEEP_JOB_ID, deduplicate=True, enqueue_after_commit=True
		)
	except Exception:
		pass  # the next tick starts it


def tick() -> None:
	"""Every minute (``reconcile.tick``): one indexed lookup, a job only when claims are open."""
	if frappe.db.exists(CLAIM, {"status": "open"}):
		enqueue_sweep()


def unreachable(exc) -> bool:
	"""DialogBrain itself did not answer (timeout, connection), as opposed to an error it
	returned for this one call."""
	if isinstance(exc, DialogBrainTimeout):
		return True
	return not any(getattr(exc, name, None) for name in ("status", "code", "error_type"))


def _settle(claim, client, now) -> bool:
	"""Ask DialogBrain about one open claim. True when it got connected."""
	past = _age_past_expiry(claim, now)
	if not claim.session_id:
		# the start call never returned a session (timeout): only ``external_ref`` can match
		# it; nothing confirms that it did not connect
		if past > GRACE_SECONDS:
			close(claim.name, "failed")
		return False
	try:
		state = client.execute(GET, {"session_id": claim.session_id}) or {}
	except DialogBrainError as exc:
		if unreachable(exc):
			if past > GIVE_UP_SECONDS:
				close(claim.name, "expired")  # not confirmed: its channels stay restricted
			raise
		# DialogBrain answered, but not about the session (it no longer knows it, the key
		# lost its rights...): this claim's problem only, and no proof that it did not connect
		if past > GRACE_SECONDS:
			close(claim.name, "expired")
		return False
	status = _s(state.get("status"))
	if status == "connected" and _s(state.get("account_id")):
		resolve(claim.name, state)
		return True
	if status == "error":
		close(claim.name, "failed", confirmed=True)
	elif status == "expired":
		# DialogBrain's own word that the session ended without connecting (a connected one
		# keeps answering ``connected``)
		close(claim.name, "expired", confirmed=True)
	elif past > GIVE_UP_SECONDS:
		close(claim.name, "expired")
	# any other status: the session is still alive in DialogBrain (someone may be typing a
	# login code in another tab, or left at the password step); it can still connect
	return False


def sweep(client=None) -> int:
	"""Settle open claims nobody is polling. Returns how many got connected.

	Bounded: a short timeout per call, the claims nearest to expiry first, and it stops
	when DialogBrain is unreachable. One failing claim does not stop the others."""
	if not frappe.db.exists(CLAIM, {"status": "open"}):
		return 0
	if not compat.single_values(SETTINGS, ["connected"]).connected:
		return 0
	if client is None:
		client = DialogBrainClient.from_settings()
		client.timeout = SWEEP_TIMEOUT
	now = now_datetime()
	connected = errors = 0
	for claim in _open(limit=SWEEP_LIMIT):
		save_point = "dialogbrain_claim_" + frappe.generate_hash(length=8)
		frappe.db.savepoint(save_point)
		try:
			connected += bool(_settle(claim, client, now))
		except DialogBrainError:  # unreachable (see _settle): the rest would wait in vain too
			errors += 1
			if errors >= SWEEP_MAX_ERRORS:
				break
		except Exception:
			frappe.db.rollback(save_point=save_point)
			frappe.log_error(title="DialogBrain: channel claim could not be settled")
	return connected
