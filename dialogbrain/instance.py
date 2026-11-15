"""Which DialogBrain instance + workspace this site is connected to (final wave F3, R13).

DialogBrain thread ids (``DB Conversation.thread_id``) are only unique within one
DialogBrain instance: a second instance (dev / prod), or the same instance reached through
another base URL, can reuse them. The *instance key* binds a conversation and the agent
registration to the connection they were made under:

``instance_key = sha1(f"{normalised base_url}|{workspace_id}")[:16]``

* ``DialogBrain Settings.dialogbrain_instance``: the key of the last Connect;
* ``DB Conversation.dialogbrain_instance``: the key when the row was created (or first
  seen without one). The agent tools identify a chat only when the two are equal.
"""

import hashlib
from urllib.parse import urlsplit

import frappe

SETTINGS = "DialogBrain Settings"
DEFAULT_PORTS = {"http": 80, "https": 443}


def normalise_url(url) -> str:
	"""``scheme://host[:port]/path`` in lower case, without a default port, a trailing
	slash, a query or a fragment. Unparseable input comes back stripped."""
	url = (url or "").strip()
	try:
		parts = urlsplit(url)
		port = parts.port
	except ValueError:
		return url.rstrip("/").lower()
	scheme = (parts.scheme or "").lower()
	host = (parts.hostname or "").lower()
	if not host:
		return url.rstrip("/").lower()
	netloc = host if port in (None, DEFAULT_PORTS.get(scheme)) else f"{host}:{port}"
	return f"{scheme}://{netloc}{parts.path.rstrip('/')}"


def instance_key(base_url, workspace_id) -> "str | None":
	"""16 hex chars for a DialogBrain base URL + workspace id; None if either is empty."""
	base, workspace = normalise_url(base_url), str(workspace_id or "").strip()
	if not base or not workspace:
		return None
	return hashlib.sha1(f"{base}|{workspace}".encode()).hexdigest()[:16]


def current_instance() -> "str | None":
	"""The instance key stored by the last Connect."""
	return frappe.db.get_single_value(SETTINGS, "dialogbrain_instance") or None
