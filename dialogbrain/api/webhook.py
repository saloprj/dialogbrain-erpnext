import json
from datetime import datetime, timedelta, timezone
from functools import partial

import frappe
from dateutil import parser as dateparser
from frappe.utils import now_datetime

from dialogbrain.sync.processor import kick_drain
from dialogbrain.sync.signature import verify_signature

MAX_BODY_BYTES = 1024 * 1024
DEFAULT_BACKFILL_DAYS = 30


def _respond(status_code: int, ok: bool, **extra):
	frappe.local.response.http_status_code = status_code
	return {"ok": ok, **extra}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive():
	"""Receive a signed DialogBrain webhook: verify, dedupe, store, enqueue.

	401 only for a bad/missing signature, 413 for bodies over 1 MB; everything
	else answers 200 so DialogBrain does not pause the subscription.
	"""
	request = frappe.request
	length = request.content_length
	if length is not None and length > MAX_BODY_BYTES:
		return _respond(413, False)
	raw = request.get_data()
	if len(raw) > MAX_BODY_BYTES:
		return _respond(413, False)

	settings = frappe.get_single("DialogBrain Settings")
	if not settings.connected:
		return {"ok": True, "dropped": "not_connected"}

	secret = settings.get_password("webhook_secret", raise_exception=False)
	header = request.headers.get("X-DialogBrain-Signature")
	if not secret or not verify_signature(raw, header, secret):
		return _respond(401, False)

	try:
		body = json.loads(raw)
	except ValueError:
		return {"ok": True, "dropped": "invalid_json"}
	if not isinstance(body, dict) or not body.get("delivery_id"):
		return {"ok": True, "dropped": "no_delivery_id"}

	delivery_id = str(body["delivery_id"])
	if frappe.db.exists("DB Webhook Log", {"delivery_id": delivery_id}):
		return {"ok": True, "duplicate": True}

	# ``skip_push`` is DialogBrain's "nobody wrote to us just now": channel-sync's backfill
	# re-emits every imported history row this way (thousands per run), and system events
	# (call markers) carry it too. History older than our own backfill window is not
	# mirrored at all; the rest of the backfill goes to ``long`` so live traffic on
	# ``short`` never waits behind it. A system event is live and stays on ``short``.
	data = body.get("data") if isinstance(body.get("data"), dict) else {}
	message = data.get("message") if isinstance(data.get("message"), dict) else {}
	backfill = data.get("skip_push") is True and message.get("is_system_event") is not True
	if backfill and _is_old_history(data, settings):
		return {"ok": True, "dropped": "history"}
	lane = "long" if backfill else "short"

	log = frappe.get_doc(
		{
			"doctype": "DB Webhook Log",
			"delivery_id": delivery_id,
			"event": body.get("event"),
			"payload": json.dumps(body),
			"received_at": now_datetime(),
			"status": "queued",
			"attempts": 0,
			"lane": lane,
		}
	)
	try:
		log.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		frappe.db.rollback()
		return {"ok": True, "duplicate": True}

	# One drain job per lane, kicked after the row is committed (so the drain can see it).
	frappe.db.after_commit.add(partial(kick_drain, lane))
	return {"ok": True}


def _is_old_history(data, settings) -> bool:
	"""``message.sent_at`` before the backfill horizon (``backfill_days``). Unparsable or
	missing dates are kept: dropping is only for what is certainly old."""
	message = data.get("message") if isinstance(data.get("message"), dict) else {}
	raw = message.get("sent_at")
	if not raw:
		return False
	try:
		sent_at = dateparser.isoparse(str(raw))
	except (ValueError, TypeError, OverflowError):
		return False
	if sent_at.tzinfo is None:
		sent_at = sent_at.replace(tzinfo=timezone.utc)
	days = int(settings.backfill_days or DEFAULT_BACKFILL_DAYS)
	return sent_at < datetime.now(timezone.utc) - timedelta(days=days)
