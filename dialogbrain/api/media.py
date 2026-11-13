"""Media proxy for message attachments (Plan 2, Task 3).

``get(message, file_id)`` checks channel access on the message's conversation and that
``file_id`` is one of *that message's* attachments, then asks DialogBrain for a fresh
presigned URL and streams the bytes to the browser. The URL is never returned or logged.

DialogBrain tool (verified against ``apps/api-gateway/src/tools/definitions/files/info.py``):
``files.info`` with ``{"file_ids": [int, ...]}`` (max 20) returns
``{"files": [{file_id, filename, mime_type, byte_size, download_url, expires_in (3600),
slide_count}], "not_found": [ids]}``.

Streaming: the method returns a werkzeug ``Response`` over a chunk generator
(``direct_passthrough``); Frappe's handler passes it through and ``sync_database`` rolls back
the (GET) transaction before the WSGI server iterates it, so the generator touches no
frappe state. The upstream ``requests`` response stays open until the generator is consumed,
and is closed in ``finally`` (or by ``call_on_close`` if the body is never iterated).
"""

import requests
import frappe
from frappe import _
from werkzeug.wrappers import Response

from dialogbrain.client import DialogBrainClient, DialogBrainError
from dialogbrain.permissions import assert_can_access

MEDIA_MAX_BYTES = 50 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
INLINE_PREFIXES = ("image/", "audio/", "video/")


class MediaTooLarge(frappe.ValidationError):
	http_status_code = 413


class MediaBadGateway(frappe.ValidationError):
	http_status_code = 502


def _bad_gateway():
	frappe.throw(_("Could not fetch the attachment from DialogBrain"), MediaBadGateway)


def _safe_filename(name):
	name = (name or "file").replace("\r", " ").replace("\n", " ").replace('"', "'")
	return name.encode("latin-1", "replace").decode("latin-1") or "file"


@frappe.whitelist(methods=["GET"])
def get(message, file_id):
	conversation = frappe.db.get_value("DB Message", message, "conversation")
	if not conversation:
		frappe.throw(_("You do not have access to this message"), frappe.PermissionError)
	assert_can_access(conversation)

	file_id = str(file_id).strip()
	att = frappe.db.get_value(
		"DB Message Attachment",
		{"parent": message, "parenttype": "DB Message", "file_id": file_id},
		["filename", "mime_type", "media_type"],
		as_dict=True,
	)
	if not att:
		frappe.throw(_("Attachment does not belong to this message"), frappe.PermissionError)
	try:
		numeric_id = int(file_id)
	except ValueError:
		frappe.throw(_("Invalid file id"), frappe.PermissionError)

	try:
		data = DialogBrainClient.from_settings().execute("files.info", {"file_ids": [numeric_id]})
		files = (data or {}).get("files") or []
		url = next((f.get("download_url") for f in files if str(f.get("file_id")) == file_id), None)
		if not url:
			raise DialogBrainError("no download url")
		upstream = requests.get(url, stream=True, timeout=30)
	except (DialogBrainError, requests.RequestException):
		_bad_gateway()

	try:
		if upstream.status_code != 200:
			_bad_gateway()
		try:
			length = int(upstream.headers.get("Content-Length") or 0)
		except ValueError:
			length = 0
		if length > MEDIA_MAX_BYTES:
			frappe.throw(_("Attachment is too large to preview"), MediaTooLarge)
	except Exception:
		upstream.close()
		raise

	mime = att.mime_type or upstream.headers.get("Content-Type") or "application/octet-stream"
	disposition = "inline" if mime.lower().startswith(INLINE_PREFIXES) else "attachment"
	headers = {
		"Content-Type": mime,
		"Cache-Control": "private, max-age=300",
		"Content-Disposition": f'{disposition}; filename="{_safe_filename(att.filename)}"',
	}
	if length:
		headers["Content-Length"] = str(length)

	def stream():
		sent = 0
		try:
			for chunk in upstream.iter_content(CHUNK_SIZE):
				if not chunk:
					continue
				sent += len(chunk)
				if sent > MEDIA_MAX_BYTES:
					raise MediaTooLarge("Attachment exceeded the size limit while streaming")
				yield chunk
		finally:
			upstream.close()

	response = Response(stream(), headers=headers, direct_passthrough=True)
	response.call_on_close(upstream.close)
	return response
