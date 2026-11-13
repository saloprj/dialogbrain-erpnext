"""Thread context of an agent tool call (Plan 3, spec §7.3).

When DialogBrain's AI agent calls an ``agent_api`` tool while answering a chat, it signs
the thread that run belongs to:

* ``X-DialogBrain-Thread-Id``: DialogBrain's internal numeric thread id
  (= ``DB Conversation.thread_id``, Plan 1 R10);
* ``X-DialogBrain-Timestamp``: unix seconds;
* ``X-DialogBrain-Context-Signature``: ``sha256=<hex>`` of
  ``HMAC-SHA256(secret, f"{timestamp}.{thread_id}.{METHOD}.{path}.{body_sha256}")``.

The key is the UTF-8 of ``Settings.thread_context_secret``. Verification uses what this
request actually carried: the raw request-URI path (no query string), the method, and the
SHA-256 of the raw body bytes (never re-serialized JSON). The timestamp must be within
±300 s, and only ``/api/method/dialogbrain.agent_api.*`` paths accept a context.

A context is *identified* only when its conversation is linked to a party with
``link_source`` ``manual`` or ``auto_verified``. Customer data is released only then.
"""

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import frappe

SETTINGS = "DialogBrain Settings"
HEADER_THREAD = "X-DialogBrain-Thread-Id"
HEADER_TIMESTAMP = "X-DialogBrain-Timestamp"
HEADER_SIGNATURE = "X-DialogBrain-Context-Signature"
SIGNATURE_PREFIX = "sha256="
MAX_SKEW = 300  # seconds
PATH_PREFIX = "/api/method/dialogbrain.agent_api."
VERIFIED_LINKS = ("manual", "auto_verified")

_DIGITS = re.compile(r"[0-9]{1,20}", re.ASCII)


@dataclass(frozen=True)
class Context:
	thread_id: str
	conversation: str
	party_type: str | None
	party: str | None
	identified: bool


def signature(secret: str, timestamp, thread_id, method: str, path: str, body: bytes) -> str:
	"""``sha256=<hex>`` for one request, per the thread-context contract."""
	body_sha256 = hashlib.sha256(body or b"").hexdigest()
	msg = f"{timestamp}.{thread_id}.{method.upper()}.{path}.{body_sha256}"
	return SIGNATURE_PREFIX + hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def verify(request=None, now=None) -> Context | None:
	"""The verified context of ``request`` (default: the current request), or None.

	None means "no trustworthy thread": no request, a missing or malformed header, a path
	outside the agent API, no secret, a stale timestamp, a bad signature or an unknown
	thread. A Context with ``identified=False`` means the thread is known but its chat is
	not linked to a verified party.
	"""
	if request is None:
		request = getattr(frappe.local, "request", None)
	if request is None:
		return None
	headers = request.headers
	thread_id = (headers.get(HEADER_THREAD) or "").strip()
	timestamp = (headers.get(HEADER_TIMESTAMP) or "").strip()
	given = (headers.get(HEADER_SIGNATURE) or "").strip()
	if not (_DIGITS.fullmatch(thread_id) and _DIGITS.fullmatch(timestamp) and given):
		return None

	path = _raw_path(request)
	if not path.startswith(PATH_PREFIX):
		return None
	now = time.time() if now is None else now
	if abs(now - int(timestamp)) > MAX_SKEW:
		return None
	secret = _secret()
	if not secret:
		return None

	expected = signature(secret, timestamp, thread_id, request.method, path, request.get_data(cache=True))
	if not hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8", "replace")):
		return None

	conv = frappe.db.get_value(
		"DB Conversation",
		{"thread_id": thread_id},
		["name", "party_type", "party", "link_source"],
		as_dict=True,
	)
	if not conv:
		return None
	identified = bool(conv.party_type and conv.party and conv.link_source in VERIFIED_LINKS)
	return Context(
		thread_id=thread_id,
		conversation=conv.name,
		party_type=conv.party_type or None,
		party=conv.party or None,
		identified=identified,
	)


def _raw_path(request) -> str:
	"""The request-URI path exactly as sent, without the query string.

	Gunicorn sets ``RAW_URI``, werkzeug's server ``REQUEST_URI``; both keep the bytes the
	client sent (``request.path`` is percent-decoded). Fall back to ``request.path``.
	"""
	environ = getattr(request, "environ", None) or {}
	raw = environ.get("RAW_URI") or environ.get("REQUEST_URI")
	if not raw:
		return request.path or ""
	if not raw.startswith("/"):  # absolute-form request target
		raw = urlsplit(raw).path or "/"
	return raw.split("?", 1)[0]


def _secret() -> str | None:
	from frappe.utils.password import get_decrypted_password

	return get_decrypted_password(SETTINGS, SETTINGS, "thread_context_secret", raise_exception=False) or None
