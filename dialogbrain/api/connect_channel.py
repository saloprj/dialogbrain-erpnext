"""Connect a WhatsApp / Telegram number from ERPNext (no DialogBrain login).

The browser talks only to these endpoints; they proxy DialogBrain's
``channels.connect_session.*`` tools with the site's key. Every connection is a
**DB Channel Claim** (``dialogbrain.claims``): only the user who started it may poll,
answer or cancel it, and the claim is what assigns the new channel to its owner.

Secrets: the phone number, the login code and the 2FA password arrive in one parameter
named ``secret`` (Frappe masks that name wherever it records request data), are passed on
once and are never stored, logged or echoed.
"""

import io
import re
from datetime import datetime, timezone

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime

from dialogbrain import claims, compat, connect_gate
from dialogbrain.client import DialogBrainClient, DialogBrainError
from dialogbrain.permissions import INBOX_ROLES, is_manager

CLAIM = "DB Channel Claim"
KINDS = ("phone", "code", "password")
WAITING = ("waiting_scan", "scanned", "need_code", "need_password", "need_pairing_code", "waiting_confirm")
START_LIMIT = (10, 600)  # starts per user per seconds ("Start again" is a start)
SUBMIT_LIMIT = (10, 600)  # submits per claim
SECRET_MAX = 200
REASON_MAX = 300
DEFAULT_TTL = 180
_KEY = re.compile(r"db_(?:live|test)_\w+")
_DATA_URL = re.compile(r"^data:image/(?:png|jpeg|gif|svg\+xml);base64,[A-Za-z0-9+/=\s]+$")

NOT_FOUND = "Connection not found"
NO_FEATURE = "Your DialogBrain server does not support connecting channels from here yet"
BILLING = "Connecting channels is not included in this DialogBrain workspace's plan"
NO_RIGHTS = "The DialogBrain key needs edit rights in the workspace to connect channels"
BUSY = "Too many attempts. Wait a few minutes and try again"
FAILED = "DialogBrain did not answer. Try again"


# --- helpers ----------------------------------------------------------------


def _require_role() -> None:
	if not set(frappe.get_roles()) & set(INBOX_ROLES):
		frappe.throw(_("You are not allowed to connect channels"), frappe.PermissionError)


def _claim(name):
	"""The caller's own claim; anyone else's looks exactly like a missing one."""
	_require_role()
	row = None
	if isinstance(name, str) and name:
		row = frappe.db.get_value(
			CLAIM,
			name,
			["name", "session_id", "requested_by", "owner_user", "channel_type", "status", "account_id", "expires_at"],
			as_dict=True,
		)
	if not row or row.requested_by != frappe.session.user:
		frappe.throw(_(NOT_FOUND), frappe.DoesNotExistError)
	return row


def _explain(exc, hide=None) -> str:
	kind = getattr(exc, "error_type", None)
	if kind == "billing_required":
		return _(BILLING)
	if kind == "permission_denied":
		return _(NO_RIGHTS)
	if kind == "rate_limited":
		return _(BUSY)
	text = _KEY.sub("[hidden]", str(exc) or type(exc).__name__)
	if hide:
		text = text.replace(hide, "[hidden]")
	return text[:REASON_MAX]


def _expires(value):
	"""DialogBrain's ``expires_at`` (ISO, usually UTC) as a naive site-time datetime."""
	try:
		parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
		if parsed.tzinfo:
			utc = parsed.astimezone(timezone.utc).replace(tzinfo=None)
			parsed = frappe.utils.convert_utc_to_system_timezone(utc).replace(tzinfo=None)
		return parsed
	except (TypeError, ValueError):
		return add_to_date(now_datetime(), seconds=DEFAULT_TTL)


def _qr(value) -> dict:
	"""``qr`` is the raw QR text (drawn here) or, from some servers, a ready image."""
	if not value or not isinstance(value, str):
		return {}
	if value.startswith("data:"):
		return {"qr_image": value} if _DATA_URL.match(value) else {}
	import pyqrcode

	buffer = io.BytesIO()
	pyqrcode.create(value, error="M").svg(buffer, scale=5, quiet_zone=3, xmldecl=False, background="#ffffff")
	return {"qr_svg": buffer.getvalue().decode("utf-8")}


def _state(claim, data=None, status=None, result=None) -> dict:
	data = data or {}
	status = status or str(data.get("status") or "")
	expires = claim.get("expires_at")
	left = int((get_datetime(expires) - now_datetime()).total_seconds()) if expires else 0
	state = {
		"claim": claim.name,
		"channel_type": claim.channel_type,
		"status": status,
		"waiting": status in WAITING,
		"expires_in": max(left, 0),
		"pairing_code": None,
		"error": None,
		"channel": None,
		"message": None,
	}
	if status in WAITING:
		state.update(_qr(data.get("qr")))
		code = data.get("pairing_code")
		state["pairing_code"] = str(code)[:20] if code else None
	if status == "error":
		state["error"] = _KEY.sub("[hidden]", str(data.get("error") or ""))[:REASON_MAX] or _(FAILED)
	if result:
		state["channel"] = result.get("channel")
		state["message"] = result.get("message")
	return state


def _owner(owner):
	"""Who the channel is for. An agent connects only for themselves; a manager may choose
	an employee, or ``""`` for a shared number."""
	user = frappe.session.user
	if not is_manager() or owner is None or owner == user:
		return user
	if owner == "":
		return None
	enabled = frappe.db.get_value("User", owner, "enabled")
	if not enabled or "Chat Agent" not in frappe.get_roles(owner):
		frappe.throw(_("Choose an enabled user with the Chat Agent role"), frappe.ValidationError)
	return owner


def _limit(key, limit) -> None:
	if compat.rate_limited(key, *limit):
		frappe.throw(_(BUSY), frappe.TooManyRequestsError)


def _settle(claim, data) -> dict:
	"""Apply DialogBrain's answer to the claim and build the state for the browser."""
	status = str(data.get("status") or "")
	if data.get("expires_at"):
		claim.expires_at = _expires(data.get("expires_at"))
		frappe.db.set_value(CLAIM, claim.name, "expires_at", claim.expires_at, update_modified=False)
	if status == "connected" and data.get("account_id"):
		return _state(claim, data, result=claims.resolve(claim.name, data))
	if status == "expired":
		claims.close(claim.name, "expired")
	elif status == "error":
		claims.close(claim.name, "failed")
	return _state(claim, data)


# --- endpoints --------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def options() -> dict:
	"""What the dialog may offer. ``channels`` is null when the feature is off."""
	_require_role()
	return {"channels": connect_gate.options(), "can_choose_owner": is_manager()}


@frappe.whitelist(methods=["POST"])
def start(channel: str, method: str = "qr", owner: "str | None" = None) -> dict:
	_require_role()
	available = connect_gate.options()
	if not available:
		frappe.throw(_(NO_FEATURE), frappe.ValidationError)
	channel = str(channel or "").strip().lower()
	method = str(method or "qr").strip().lower()
	if method not in (available.get(channel) or ()):
		frappe.throw(_("This channel or method is not available"), frappe.ValidationError)
	owner_user = _owner(owner)
	_limit(f"connect_start:{frappe.session.user}", START_LIMIT)

	doc = frappe.get_doc(
		{
			"doctype": CLAIM,
			"requested_by": frappe.session.user,
			"owner_user": owner_user,
			"channel_type": channel,
			"method": method,
			"status": "open",
			"expires_at": add_to_date(now_datetime(), seconds=DEFAULT_TTL),
		}
	)
	doc.insert(ignore_permissions=True)
	reason = None
	try:
		data = DialogBrainClient.from_settings().execute(
			connect_gate.START, {"channel": channel, "method": method, "external_ref": doc.name}
		) or {}
	except DialogBrainError as exc:
		reason = _explain(exc)
	if reason is None and not data.get("session_id"):
		reason = _(FAILED)
	if reason is not None:
		frappe.db.set_value(CLAIM, doc.name, {"status": "failed", "resolved_at": now_datetime()})
		frappe.db.commit()  # the failed claim is kept; frappe.throw rolls nothing else back
		frappe.throw(reason, frappe.ValidationError)
	doc.session_id = str(data.get("session_id"))
	doc.expires_at = _expires(data.get("expires_at"))
	frappe.db.set_value(CLAIM, doc.name, {"session_id": doc.session_id, "expires_at": doc.expires_at})
	return _state(doc, data)


@frappe.whitelist(methods=["POST"])
def get(claim: str) -> dict:
	row = _claim(claim)
	if row.status != "open":
		if row.status == "connected":
			return _state(row, status="connected", result=claims.resolve(row.name, {}))
		return _state(row, status="expired" if row.status in ("expired", "cancelled") else "error")
	try:
		data = DialogBrainClient.from_settings().execute(connect_gate.GET, {"session_id": row.session_id}) or {}
	except DialogBrainError as exc:
		frappe.throw(_explain(exc), frappe.ValidationError)
	return _settle(row, data)


@frappe.whitelist(methods=["POST"])
def submit(claim: str, kind: str, secret=None) -> dict:
	"""Send the phone number, the login code or the 2FA password of this connection.

	``secret`` has no type annotation on purpose: Frappe's argument validation quotes a
	value of the wrong type in its error."""
	frappe.local.form_dict.pop("secret", None)
	row = _claim(claim)
	if kind not in KINDS:
		frappe.throw(_("Unknown input"), frappe.ValidationError)
	if not isinstance(secret, str) or not secret.strip() or len(secret) > SECRET_MAX:
		frappe.throw(_("Enter a value"), frappe.ValidationError)
	if row.status != "open":
		frappe.throw(_(NOT_FOUND), frappe.DoesNotExistError)
	_limit(f"connect_submit:{row.name}", SUBMIT_LIMIT)
	secret = secret.strip()
	reason, data = None, {}
	try:
		data = DialogBrainClient.from_settings().execute(
			connect_gate.SUBMIT, {"session_id": row.session_id, kind: secret}
		) or {}
	except DialogBrainError as exc:
		reason = _explain(exc, hide=secret)
	except Exception as exc:  # nothing of this request may reach the Error Log
		reason = _(FAILED) + f" ({type(exc).__name__})"
	secret = None
	if reason is not None:
		frappe.throw(reason, frappe.ValidationError)
	return _settle(row, data)


@frappe.whitelist(methods=["POST"])
def cancel(claim: str) -> dict:
	"""Stop a connection that is still waiting. One that already connected is left alone:
	closing the dialog must never disconnect a number."""
	row = _claim(claim)
	if row.status != "open":
		return {"ok": True}
	try:
		client = DialogBrainClient.from_settings()
		data = client.execute(connect_gate.GET, {"session_id": row.session_id}) or {}
		if str(data.get("status") or "") == "connected" and data.get("account_id"):
			claims.resolve(row.name, data)
			return {"ok": True}
		if str(data.get("status") or "") in WAITING:
			client.execute(connect_gate.CANCEL, {"session_id": row.session_id})
	except DialogBrainError:
		pass  # the session ends by itself in a few minutes
	claims.close(row.name, "cancelled")
	return {"ok": True}
