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
±300 s, and only ``/api/method/dialogbrain.agent_api.*`` paths without a query string
accept a context (query args are not signed; Ruling R9).

A conversation stamped with another DialogBrain instance than the current connection's
(``dialogbrain_instance``, final wave F3b) is treated like an unknown thread
(``other_instance``).

A context is *identified* only when its conversation is linked to an existing Customer or
Lead with ``link_source`` ``manual`` or ``auto_verified``. Customer data is released only
then. :func:`resolve` also gives a reason code for the call log (Ruling R8).
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
PARTY_TYPES = ("Customer", "Lead")

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


# Reason codes for a call that is not identified (Ruling R8). They go to the call log
# only, never to the caller.
NO_HEADERS = "no_headers"
BAD_SIGNATURE = "bad_signature"
STALE = "stale"
UNKNOWN_THREAD = "unknown_thread"
UNLINKED = "unlinked"
UNVERIFIED = "unverified"
QUERY_STRING = "query_string"
NO_SECRET = "no_secret"
# The conversation was made under another DialogBrain instance / workspace (final wave
# F3b): its thread id may name a different chat here.
OTHER_INSTANCE = "other_instance"


def verify(request=None, now=None) -> Context | None:
	"""The verified context of ``request`` (default: the current request), or None.

	None means "no trustworthy thread": no request, a query string, a missing or malformed
	header, a path outside the agent API, no secret, a stale timestamp, a bad signature or
	an unknown thread. A Context with ``identified=False`` means the thread is known but
	its chat is not linked to a verified party.
	"""
	return resolve(request, now)[0]


def resolve(request=None, now=None) -> tuple[Context | None, str | None]:
	"""``(context, reason)``: what :func:`verify` returns, plus a reason code when the call
	is not identified (``None`` when it is)."""
	if request is None:
		request = getattr(frappe.local, "request", None)
	if request is None:
		return None, NO_HEADERS
	if query_string(request):  # Ruling R9: query args are not signed
		return None, QUERY_STRING
	headers = request.headers
	thread_id = (headers.get(HEADER_THREAD) or "").strip()
	timestamp = (headers.get(HEADER_TIMESTAMP) or "").strip()
	given = (headers.get(HEADER_SIGNATURE) or "").strip()
	if not (thread_id and timestamp and given):
		return None, NO_HEADERS
	if not (_DIGITS.fullmatch(thread_id) and _DIGITS.fullmatch(timestamp)):
		return None, BAD_SIGNATURE

	path = _raw_path(request)
	if not path.startswith(PATH_PREFIX):
		return None, BAD_SIGNATURE
	now = time.time() if now is None else now
	if abs(now - int(timestamp)) > MAX_SKEW:
		return None, STALE
	secret = _secret()
	if not secret:
		return None, NO_SECRET

	# Frappe's make_form_dict calls get_data() before touching .form, so werkzeug has
	# cached the raw bytes for JSON and form bodies alike; this is what was sent.
	expected = signature(secret, timestamp, thread_id, request.method, path, request.get_data(cache=True))
	if not hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8", "replace")):
		return None, BAD_SIGNATURE

	conv = frappe.db.get_value(
		"DB Conversation",
		{"thread_id": thread_id},
		["name", "party_type", "party", "link_source", "dialogbrain_instance"],
		as_dict=True,
	)
	if not conv:
		return None, UNKNOWN_THREAD
	from dialogbrain.instance import current_instance

	if (conv.dialogbrain_instance or "") != (current_instance() or ""):
		return None, OTHER_INSTANCE
	linked = bool(
		conv.party_type in PARTY_TYPES and conv.party and frappe.db.exists(conv.party_type, conv.party)
	)
	verified = conv.link_source in VERIFIED_LINKS
	ctx = Context(
		thread_id=thread_id,
		conversation=conv.name,
		party_type=conv.party_type or None,
		party=conv.party or None,
		identified=linked and verified,
	)
	if not linked:
		return ctx, UNLINKED
	if not verified:
		return ctx, UNVERIFIED
	return ctx, None


def query_string(request) -> str:
	"""The request's query string (``QUERY_STRING`` or the part of the raw URI after
	``?``); empty when there is none."""
	environ = getattr(request, "environ", None) or {}
	qs = environ.get("QUERY_STRING") or ""
	raw = environ.get("RAW_URI") or environ.get("REQUEST_URI") or ""
	if "?" in raw:
		qs = qs or raw.split("?", 1)[1]
	return qs


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
