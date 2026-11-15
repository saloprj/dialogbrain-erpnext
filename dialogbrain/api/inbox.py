"""Whitelisted inbox API (send-related methods; the rest of the inbox comes in Plan 2)."""

import frappe

from dialogbrain.sync.sender import create_reply, retry_reply


@frappe.whitelist(methods=["POST"])
def send(conversation: str, text: str, client_ref: str, file_urls: "list[str] | None" = None) -> dict:
	"""Queue a manager reply. Idempotent per ``client_ref``.

	``file_urls``: Frappe File URLs the user can read (a list, or its JSON string as
	posted by the browser; the annotation is a string on purpose so Frappe's pydantic
	argument validation does not reject the JSON string form).

	Returns ``{"name", "delivery_status"}`` of the DB Message (``pending`` on first call).
	"""
	return create_reply(conversation, text, client_ref, file_urls)


@frappe.whitelist(methods=["POST"])
def retry(message: str) -> None:
	"""Re-deliver a ``failed`` reply."""
	retry_reply(message)
