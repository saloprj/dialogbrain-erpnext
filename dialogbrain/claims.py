"""Who owns a number that is being connected from ERPNext.

``api/connect_channel.start`` stores a **DB Channel Claim**: the DialogBrain connect
session, who started it and who the channel is for. The claim is what gives the new
DB Channel its ``allowed_users``, and it does so from three places, so that the owner is
set even when the browser tab is closed right after the scan:

* the browser poll (``api/connect_channel.get``) on ``connected``;
* ``sweep`` in the every-minute tick, for claims nobody is polling;
* ``on_new_channel``, called by ``upsert_channel`` when a webhook or the backfill is about
  to create a channel while a claim of that type is open.

A DB Channel without allowed users is visible to every Chat Agent. A channel that might be
the number of an open claim is therefore never created without them: when it cannot be
matched yet it is created *closed* (the owners of the open claims), recorded on those
claims as provisional, and opened or re-assigned once the claims are settled.

No phone number, code or password passes through here.
"""

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime

from dialogbrain import compat
from dialogbrain.client import DialogBrainClient, DialogBrainError
from dialogbrain.connect_gate import GET

CLAIM = "DB Channel Claim"
CHANNEL = "DB Channel"
SETTINGS = "DialogBrain Settings"
GRACE_SECONDS = 600
MAX_LOOKUPS = 3
LOOKUP_TIMEOUT = 5
SWEEP_LIMIT = 20
TAKEN = "This number is already assigned to another employee"
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


def _client(timeout=None):
	client = DialogBrainClient.from_settings()
	if timeout:
		client.timeout = timeout
	return client


# --- channel users ----------------------------------------------------------


def _allowed(channel) -> list:
	return frappe.get_all(
		"DB Channel User",
		filters={"parent": channel, "parenttype": CHANNEL},
		pluck="user",
		order_by="idx",
	)


def _set_allowed(channel, users) -> None:
	doc = frappe.get_doc(CHANNEL, channel)
	doc.set("allowed_users", [{"user": user} for user in dict.fromkeys(u for u in users if u)])
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)


def _provisional(claim) -> list:
	return [c for c in (claim.get("provisional_channels") or "").split("\n") if c]


def _release_provisional(claim, keep=None) -> None:
	"""The claim is settled: channels closed for its owner "just in case" lose that owner,
	except ``keep`` (the channel the claim turned out to be)."""
	for channel in _provisional(claim):
		if channel == keep or not frappe.db.exists(CHANNEL, channel):
			continue
		users = _allowed(channel)
		if claim.owner_user in users:
			_set_allowed(channel, [u for u in users if u != claim.owner_user])
	if claim.get("provisional_channels"):
		frappe.db.set_value(CLAIM, claim.name, "provisional_channels", None, update_modified=False)


def _close(claim, status, account_id=None, keep=None) -> None:
	values = {"status": status, "resolved_at": now_datetime()}
	if account_id:
		values["account_id"] = account_id
	frappe.db.set_value(CLAIM, claim.name, values)
	_release_provisional(claim, keep=keep)


def close(claim_name, status) -> None:
	"""Mark an open claim ``expired`` / ``cancelled`` / ``failed``."""
	claim = frappe.db.get_value(CLAIM, claim_name, _FIELDS, as_dict=True, for_update=True)
	if claim and claim.status == "open":
		_close(claim, status)


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
	that names nobody) when the number already belongs to someone else."""
	from dialogbrain.sync.upsert import upsert_channel

	claim = frappe.db.get_value(CLAIM, claim_name, _FIELDS, as_dict=True, for_update=True)
	if not claim:
		return {"channel": None, "assigned": False, "message": None}
	account_id = _s((state or {}).get("account_id")) or _s(claim.account_id)
	if not account_id:
		return {"channel": None, "assigned": False, "message": None}
	owner = claim.owner_user
	was_provisional = account_id in _provisional(claim)
	existed = bool(frappe.db.exists(CHANNEL, account_id))
	title = _s((state or {}).get("name")) or _s((state or {}).get("phone")) or None
	channel = upsert_channel(account_id, claim.channel_type, title, check_claims=False)
	users = _allowed(channel)

	assigned, message = True, None
	if not existed or was_provisional:
		wanted = [owner] if owner else []
		if users != wanted:
			_set_allowed(channel, wanted)
	elif not users:
		if owner:
			_set_allowed(channel, [owner])
	elif owner and owner in users:
		pass
	else:
		assigned, message = False, _(TAKEN)

	if claim.status != "connected":
		_close(claim, "connected", account_id=account_id, keep=channel)
		announce(channel)
	return {"channel": channel, "assigned": assigned, "message": message}


# --- a channel appears before its claim is resolved -------------------------


def _open(channel_type=None, limit=None) -> list:
	filters = {"status": "open"}
	if channel_type:
		filters["channel_type"] = channel_type
	return frappe.get_all(CLAIM, filters=filters, fields=_FIELDS, order_by="creation desc", limit=limit)


def _past_grace(claim, now=None) -> bool:
	if not claim.expires_at:
		return False
	return (now or now_datetime()) > add_to_date(get_datetime(claim.expires_at), seconds=GRACE_SECONDS)


def on_new_channel(account_id, channel_type, external_ref=None, client=None) -> list:
	"""Users to put in ``allowed_users`` of a DB Channel that is about to be created.

	``[]`` means "nobody is connecting such a number": the channel is open as before."""
	channel_type = _type(channel_type)
	if not channel_type or not frappe.db.exists(CLAIM, {"status": "open", "channel_type": channel_type}):
		return []
	account_id = _s(account_id)
	now = now_datetime()
	candidates = [c for c in _open(channel_type) if not _past_grace(c, now)]
	if not candidates:
		return []

	match = next((c for c in candidates if external_ref and c.name == _s(external_ref)), None)
	if not match:
		for claim in candidates[:MAX_LOOKUPS]:
			if not claim.session_id:
				continue
			try:
				client = client or _client(LOOKUP_TIMEOUT)
				state = client.execute(GET, {"session_id": claim.session_id}) or {}
			except Exception:
				continue
			if _s(state.get("account_id")) == account_id:
				match = claim
				break
	if match:
		# upsert_channel inserts the row right after this (and announces it).
		_close(match, "connected", account_id=account_id, keep=account_id)
		return [match.owner_user] if match.owner_user else []

	# Not matched yet: closed for the owners of the open claims until they are settled.
	owners = list(dict.fromkeys(c.owner_user for c in candidates if c.owner_user))
	if not owners:
		return []  # only shared numbers are being connected: open is what they asked for
	for claim in candidates:
		if claim.owner_user:
			channels = [*_provisional(claim), account_id]
			frappe.db.set_value(
				CLAIM, claim.name, "provisional_channels", "\n".join(dict.fromkeys(channels)), update_modified=False
			)
	return owners


# --- sweep ------------------------------------------------------------------


def sweep(client=None) -> int:
	"""Every minute: settle open claims nobody is polling. Returns how many got connected."""
	if not frappe.db.exists(CLAIM, {"status": "open"}):
		return 0
	if not compat.single_values(SETTINGS, ["connected"]).connected:
		return 0
	now = now_datetime()
	connected = 0
	for claim in _open(limit=SWEEP_LIMIT):
		if _past_grace(claim, now) or not claim.session_id:
			close(claim.name, "expired" if claim.session_id else "failed")
			continue
		try:
			client = client or _client()
			state = client.execute(GET, {"session_id": claim.session_id}) or {}
		except DialogBrainError:
			continue
		status = _s(state.get("status"))
		if status == "connected" and _s(state.get("account_id")):
			resolve(claim.name, state)
			connected += 1
		elif status == "error":
			close(claim.name, "failed")
		elif status == "expired":
			# a session that connected keeps answering ``connected``: this one never did
			close(claim.name, "expired")
	return connected
