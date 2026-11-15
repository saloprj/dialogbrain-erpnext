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
and is closed in the generator's ``finally``.
"""

import re
from urllib.parse import quote

import requests

import frappe
from frappe import _
from werkzeug.wrappers import Response

from dialogbrain.client import DialogBrainClient, DialogBrainError
from dialogbrain.permissions import assert_can_access

MEDIA_MAX_BYTES = 50 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
# Served inline only when the normalized mime is on this list; everything else (svg, html,
# xml, text/*, malformed types) is a download, so it can never run script on our origin.
INLINE_MIMES = frozenset(
	(
		"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/avif",
		"audio/mpeg", "audio/ogg", "audio/wav", "audio/webm", "audio/aac", "audio/mp4",
		"audio/x-m4a",
		"video/mp4", "video/webm", "video/ogg", "video/quicktime",
	)
)


_MIME_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")


def _normalize_mime(value):
	"""``type/subtype`` lower-cased without parameters; anything malformed (spaces, CR/LF,
	no subtype) becomes ``application/octet-stream``."""
	mime = str(value or "").split(";", 1)[0].strip().lower()
	return mime if _MIME_RE.match(mime) else "application/octet-stream"


class MediaTooLarge(frappe.ValidationError):
	http_status_code = 413


class MediaBadGateway(frappe.ValidationError):
	http_status_code = 502
	# Frappe's error snapshot would record frame locals and the chained exception, which
	# carry the presigned URL.
	skip_error_log = True


def _bad_gateway():
	frappe.throw(_("Could not fetch the attachment from DialogBrain"), MediaBadGateway)


_RANGE_RE = re.compile(r"^bytes=([0-9]*)-([0-9]*)$")


_CONTENT_RANGE_RE = re.compile(r"^bytes [0-9]+-[0-9]+/([0-9]+|\*)$")
_UNSATISFIED_RE = re.compile(r"^bytes \*/[0-9]+$")


def _requested_range():
	"""The client's single ``bytes=a-b | a- | -n`` range, or None. The value forwarded
	upstream is rebuilt from the regex groups, never the raw header."""
	try:
		headers = frappe.request.headers
	except (RuntimeError, AttributeError):  # no request bound (tests, background calls)
		return None
	raw = headers.get("Range") if headers else None
	match = _RANGE_RE.fullmatch(raw.strip()) if isinstance(raw, str) else None
	if not match or not (match.group(1) or match.group(2)):
		return None
	return f"bytes={match.group(1)}-{match.group(2)}"


def _safe_filename(name):
	name = (name or "file").replace("\r", " ").replace("\n", " ").replace('"', "'")
	return name.encode("latin-1", "replace").decode("latin-1") or "file"


def _disposition(kind, filename):
	return f"{kind}; filename=\"{_safe_filename(filename)}\"; filename*=UTF-8''{quote(filename or 'file', safe='')}"


def _open_upstream(file_id, numeric_id, range_value=None):
	"""Resolve the URL and open the stream. Returns None on any failure; the URL never
	leaves this frame (the caller raises outside any except block)."""
	try:
		data = DialogBrainClient.from_settings().execute("files.info", {"file_ids": [numeric_id]})
		files = data.get("files") if isinstance(data, dict) else None
		if not isinstance(files, list):
			return None
		url = next(
			(f.get("download_url") for f in files
			 if isinstance(f, dict) and str(f.get("file_id")) == file_id),
			None,
		)
		if not url:
			return None
		if range_value:
			return requests.get(url, stream=True, timeout=30, headers={"Range": range_value})
		return requests.get(url, stream=True, timeout=30)
	except (DialogBrainError, requests.RequestException):
		return None


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

	range_value = _requested_range()
	upstream = _open_upstream(file_id, numeric_id, range_value)
	if upstream is None:
		_bad_gateway()

	try:
		if upstream.status_code not in (200, 206, 416):
			_bad_gateway()
		# 206/416 are only meaningful as an answer to a Range we forwarded, and a 206 must
		# say which bytes it carries.
		if upstream.status_code in (206, 416) and not range_value:
			_bad_gateway()
		if upstream.status_code == 206 and not _CONTENT_RANGE_RE.fullmatch(
				str(upstream.headers.get("Content-Range") or "")):
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

	status = upstream.status_code
	normalized = _normalize_mime(att.mime_type or upstream.headers.get("Content-Type"))
	kind = "inline" if normalized in INLINE_MIMES else "attachment"
	headers = {
		# never the raw value: it comes from the channel and may carry CR/LF or parameters
		"Content-Type": normalized,
		"Cache-Control": "private, max-age=300",
		"Content-Disposition": _disposition(kind, att.filename),
		"X-Content-Type-Options": "nosniff",
		"Content-Security-Policy": "sandbox; default-src 'none'",
	}
	if kind == "inline" and normalized.startswith(("audio/", "video/")):
		headers["Accept-Ranges"] = "bytes"
	if status == 416:
		content_range = str(upstream.headers.get("Content-Range") or "")
		if _UNSATISFIED_RE.fullmatch(content_range):
			headers["Content-Range"] = content_range
		headers["Cache-Control"] = "no-store"
		upstream.close()
		return Response(b"", status=416, headers=headers)
	if status == 206:
		headers["Content-Range"] = str(upstream.headers["Content-Range"])
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

	return Response(stream(), status=status, headers=headers, direct_passthrough=True)
