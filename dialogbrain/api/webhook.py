import json

import frappe
from frappe.utils import now_datetime

from dialogbrain.sync.signature import verify_signature

MAX_BODY_BYTES = 1024 * 1024
PROCESSOR = "dialogbrain.sync.processor.process_delivery"


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

	log = frappe.get_doc(
		{
			"doctype": "DB Webhook Log",
			"delivery_id": delivery_id,
			"event": body.get("event"),
			"payload": json.dumps(body),
			"received_at": now_datetime(),
			"status": "queued",
			"attempts": 0,
		}
	)
	try:
		log.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		frappe.db.rollback()
		return {"ok": True, "duplicate": True}

	frappe.enqueue(
		PROCESSOR,
		queue="short",
		enqueue_after_commit=True,
		log_name=log.name,
	)
	return {"ok": True}
