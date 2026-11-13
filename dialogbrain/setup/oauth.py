"""OAuth 2.1 (authorization code + PKCE, public client) connect to DialogBrain.

The protocol helpers (``discover`` … ``redirect_allowed``) are pure functions with no
Frappe document access. ``start`` / ``callback`` / ``pop_result`` drive the flow from the
DialogBrain Settings form. Secrets (authorization code, PKCE verifier, returned API key)
never appear in a URL ERPNext builds, an error message, a log or a stored result.
"""

import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import frappe
import requests
from frappe import _

from dialogbrain.client import DialogBrainClient, DialogBrainError, mask

TIMEOUT = 20
KEY_PREFIX = "db_"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

CALLBACK_PATH = "/api/method/dialogbrain.setup.oauth.callback"
SETTINGS_PATH = "/app/dialogbrain-settings"
STATE_TTL = 600
RESULT_TTL = 600
ISSUER_CONF = "dialogbrain_oauth_issuer"
_STATE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")


def _scrub(text, *secrets_):
	text = str(text)
	for s in secrets_:
		if s:
			text = text.replace(s, "…")
	return text


def _error_code(resp):
	"""Short OAuth error code from a response, or '' (the body itself is never echoed)."""
	try:
		code = (resp.json() or {}).get("error")
	except Exception:
		return ""
	return code if isinstance(code, str) and _ERROR_CODE.match(code) else ""


def _json(resp):
	try:
		data = resp.json()
	except Exception:
		return None
	return data if isinstance(data, dict) else None


def discover(base_url: str) -> dict:
	url = (base_url or "").rstrip("/") + "/.well-known/oauth-authorization-server"
	try:
		resp = requests.get(url, timeout=TIMEOUT)
	except requests.RequestException as e:
		raise DialogBrainError(f"Cannot reach DialogBrain at {base_url}: {type(e).__name__}") from None
	if resp.status_code != 200:
		raise DialogBrainError(f"DialogBrain OAuth discovery failed (HTTP {resp.status_code})", status=resp.status_code)
	meta = _json(resp)
	if not meta:
		raise DialogBrainError("DialogBrain OAuth discovery returned an invalid response")
	for field in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
		if not meta.get(field):
			raise DialogBrainError(f"DialogBrain OAuth metadata is missing {field}")
	if "S256" not in (meta.get("code_challenge_methods_supported") or []):
		raise DialogBrainError("DialogBrain OAuth server does not support PKCE S256")
	_check_endpoints(base_url, meta)
	return meta


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url) -> "tuple | None":
	"""``(scheme, host, port)`` of an http(s) URL with a host, lowercased; else None."""
	if not isinstance(url, str):
		return None
	try:
		parts = urlparse(url.strip())
		scheme = parts.scheme.lower()
		host = (parts.hostname or "").lower()
		port = parts.port
	except ValueError:
		return None
	if scheme not in _DEFAULT_PORTS or not host:
		return None
	return scheme, host, port or _DEFAULT_PORTS[scheme]


def _check_endpoints(base_url: str, meta: dict) -> None:
	"""The browser is sent to ``authorization_endpoint``; the code, verifier and key go to
	``token_endpoint`` / ``registration_endpoint``. Refuse metadata that points elsewhere."""
	base = _origin(base_url)
	if not base:
		raise DialogBrainError("DialogBrain base URL must be an http(s) address")
	auth = _origin(meta["authorization_endpoint"])
	if not auth:
		raise DialogBrainError("DialogBrain OAuth metadata has an invalid authorization_endpoint")
	if auth[0] != "https" and base[0] != "http" and auth[1] not in LOOPBACK_HOSTS:
		raise DialogBrainError("DialogBrain OAuth authorization_endpoint must use https")
	for field in ("token_endpoint", "registration_endpoint"):
		if _origin(meta[field]) != base:
			raise DialogBrainError(f"DialogBrain OAuth {field} is not on the DialogBrain base URL")


def register_client(registration_endpoint: str, redirect_uri: str, client_name: str) -> str:
	try:
		resp = requests.post(
			registration_endpoint,
			json={"redirect_uris": [redirect_uri], "client_name": client_name},
			timeout=TIMEOUT,
		)
	except requests.RequestException as e:
		raise DialogBrainError(f"Cannot register with DialogBrain: {type(e).__name__}") from None
	data = _json(resp) if resp.status_code in (200, 201) else None
	if not data or not data.get("client_id"):
		raise DialogBrainError(
			f"DialogBrain client registration failed (HTTP {resp.status_code})", status=resp.status_code
		)
	return data["client_id"]


def pkce_pair():
	verifier = secrets.token_urlsafe(64)  # 86 chars
	digest = hashlib.sha256(verifier.encode()).digest()
	return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(authorization_endpoint, client_id, redirect_uri, state, challenge) -> str:
	parts = urlparse(authorization_endpoint)
	query = parse_qsl(parts.query, keep_blank_values=True)
	query += [
		("response_type", "code"),
		("client_id", client_id),
		("redirect_uri", redirect_uri),
		("state", state),
		("code_challenge", challenge),
		("code_challenge_method", "S256"),
		("scope", "mcp"),
	]
	return urlunparse(parts._replace(query=urlencode(query)))


def exchange_code(token_endpoint, code, verifier, client_id, redirect_uri) -> str:
	try:
		resp = requests.post(
			token_endpoint,
			data={
				"grant_type": "authorization_code",
				"code": code,
				"redirect_uri": redirect_uri,
				"client_id": client_id,
				"code_verifier": verifier,
			},
			timeout=TIMEOUT,
		)
	except requests.RequestException as e:
		raise DialogBrainError(_scrub(f"Token exchange failed: {type(e).__name__}", code, verifier)) from None
	data = _json(resp) if resp.status_code == 200 else None
	if data is None:
		reason = _error_code(resp)
		msg = f"DialogBrain token exchange failed (HTTP {resp.status_code})" + (f": {reason}" if reason else "")
		raise DialogBrainError(_scrub(msg, code, verifier), status=resp.status_code)
	token = data.get("access_token")
	if not isinstance(token, str) or not token.startswith(KEY_PREFIX):
		raise DialogBrainError("DialogBrain token exchange did not return an API key")
	return token


def redirect_allowed(uri: str) -> bool:
	try:
		parts = urlparse(uri)
		host = parts.hostname
	except ValueError:
		return False
	if parts.scheme == "https":
		return bool(host)
	return parts.scheme == "http" and host in LOOPBACK_HOSTS


# --- flow (DialogBrain Settings) ----------------------------------------------------


def _state_key(state: str) -> str:
	return f"dialogbrain:oauth:{state}"


def _result_key(user: str) -> str:
	return f"dialogbrain:oauth_result:{user}"


@frappe.whitelist(methods=["POST"])
def start() -> dict:
	"""Begin "Connect with DialogBrain": returns the DialogBrain consent URL to open."""
	from dialogbrain.setup import connect as conn

	frappe.only_for(conn.ROLE)
	doc = frappe.get_single(conn.SETTINGS)
	if not doc.base_url:
		frappe.throw(_("Set the DialogBrain base URL first"))
	redirect_uri = frappe.utils.get_url(CALLBACK_PATH)
	if not redirect_allowed(redirect_uri):
		frappe.throw(
			_('Sign-in with DialogBrain needs an https site address. Use "Connect with API key" instead.')
		)
	try:
		meta = discover(doc.base_url)
	except DialogBrainError as exc:
		frappe.throw(_("Cannot start sign-in with DialogBrain: {0}").format(_safe_message(exc)))
	issuer = frappe.conf.get(ISSUER_CONF) or meta.get("issuer")
	if not issuer:
		frappe.throw(_("Cannot start sign-in with DialogBrain: its OAuth metadata has no issuer"))

	client_id = doc.oauth_client_id
	# R1b: a client is only valid for the redirect URI and the DialogBrain it was registered with
	if not client_id or doc.oauth_redirect_uri != redirect_uri or doc.oauth_base_url != doc.base_url:
		host = urlparse(redirect_uri).hostname
		try:
			client_id = register_client(meta["registration_endpoint"], redirect_uri, f"ERPNext — {host}")
		except DialogBrainError as exc:
			frappe.throw(_("Cannot register this site with DialogBrain: {0}").format(_safe_message(exc)))
		doc.oauth_client_id = client_id
		doc.oauth_redirect_uri = redirect_uri
		doc.oauth_base_url = doc.base_url
		conn._server_save(doc)

	verifier, challenge = pkce_pair()
	state = secrets.token_urlsafe(32)
	entry = {
		"user": frappe.session.user,
		"verifier": verifier,
		"redirect_uri": redirect_uri,
		"client_id": client_id,
		"token_endpoint": meta["token_endpoint"],
		"issuer": issuer,
	}
	frappe.cache().set_value(_state_key(state), entry, expires_in_sec=STATE_TTL)
	return {"authorize_url": authorize_url(meta["authorization_endpoint"], client_id, redirect_uri, state, challenge)}


def _claim(state) -> "dict | None":
	"""Read the pending entry for ``state`` and consume it; only one caller ever wins."""
	if not isinstance(state, str) or not _STATE.match(state):
		return None
	cache = frappe.cache()
	key = _state_key(state)
	entry = cache.get_value(key, expires=True)
	if not entry:
		return None
	# raw Redis DEL returns how many keys it removed: exactly one concurrent caller gets 1
	if cache.delete(cache.make_key(key)) != 1:
		return None
	return entry


def _store_result(user: str, ok: bool, message: str, **extra) -> None:
	result = {"ok": ok, "message": message}
	result.update(extra)
	frappe.cache().set_value(_result_key(user), result, expires_in_sec=RESULT_TTL)


def _refuse(user: str, message: str) -> None:
	_store_result(user, False, message)


def _safe_message(exc, *secret_values) -> str:
	text = str(exc) or type(exc).__name__
	for s in secret_values:
		if s:
			text = text.replace(s, mask(s) if s.startswith(KEY_PREFIX) else "…")
	return text[:300]


def _redirect(location: str) -> None:
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = location


def _workspace_switch(doc, key) -> "str | None":
	"""Refusal text when ``key`` belongs to another workspace than the connected one."""
	workspace = DialogBrainClient(doc.base_url, key).execute("workspace.current", {}) or {}
	new_id = str(workspace.get("id") or "")
	if new_id == str(doc.workspace_id or ""):
		return None
	current = str(doc.workspace_name or doc.workspace_id or "")[:100]
	new = str(workspace.get("name") or new_id)[:100]
	return _("This site is connected to {0}. Disconnect first to switch to {1}.").format(current, new)


def _restore_key(conn, old_key) -> None:
	doc = frappe.get_single(conn.SETTINGS)
	doc.api_key = old_key
	conn._server_save(doc)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def callback(code=None, state=None, error=None, iss=None, **kwargs) -> None:
	"""DialogBrain redirects the admin's browser here. Every outcome is a redirect."""
	from urllib.parse import quote

	from dialogbrain.setup import connect as conn

	user = frappe.session.user
	if user == "Guest":
		# session expired during consent: log in and come back; the state stays unclaimed
		_redirect("/login?redirect-to=" + quote(frappe.request.full_path, safe=""))
		return
	_redirect(SETTINGS_PATH)

	entry = _claim(state)
	if not entry:
		return _refuse(user, _("DialogBrain sign-in failed: the request expired or was already used. Try again."))
	if entry.get("user") != user:
		return _refuse(user, _("DialogBrain sign-in was started by another user. Try again."))
	if conn.ROLE not in frappe.get_roles():
		return _refuse(user, _("Only a System Manager can connect DialogBrain."))
	if error:
		return _refuse(user, _("DialogBrain sign-in was cancelled or refused: {0}").format(str(error)[:100]))
	if not iss or iss != entry.get("issuer"):
		return _refuse(user, _("DialogBrain sign-in failed: unexpected issuer. Try again."))
	if not code:
		return _refuse(user, _("DialogBrain sign-in failed: no authorization code. Try again."))

	verifier = entry.get("verifier")
	key = None
	try:
		key = exchange_code(entry["token_endpoint"], code, verifier, entry["client_id"], entry["redirect_uri"])
		doc = frappe.get_single(conn.SETTINGS)
		was_connected = bool(doc.connected)
		old_key = doc.get_password("api_key", raise_exception=False) if was_connected else None
		if was_connected:
			# R4b: a reconnect must not silently move a connected site to another workspace
			switch = _workspace_switch(doc, key)
			if switch:
				return _refuse(user, switch)
		doc.api_key = key
		filled = {}
		if (doc.network_mode or "public") == "public" and not doc.public_site_url:
			doc.public_site_url = frappe.utils.get_url()
			filled["public_site_url_filled"] = doc.public_site_url
		conn._server_save(doc)
		# a GET request is rolled back unless asked to commit: keep the key (and, if it
		# works, the connection) even when Connect itself fails below
		frappe.local.flags.commit = True
		frappe.db.savepoint("dialogbrain_oauth_connect")
		try:
			res = conn._connect(doc)
		except Exception:
			frappe.db.rollback(save_point="dialogbrain_oauth_connect")
			if was_connected and old_key:
				# the existing connection stays as it was (Review Focus 4)
				_restore_key(conn, old_key)
			raise
		_store_result(
			user,
			True,
			_("Connected to DialogBrain workspace {0}").format(res.get("workspace") or ""),
			**res,
			**filled,
		)
	except (DialogBrainError, frappe.ValidationError) as exc:
		_refuse(user, _safe_message(exc, key, code, verifier))
	except Exception:  # noqa: BLE001 — every outcome must end in a redirect, never an error page
		# deferred: a GET request's transaction is rolled back, a plain insert would vanish
		frappe.log_error(
			title="DialogBrain OAuth callback failed",
			message=_scrub(frappe.get_traceback(), key, code, verifier),
			defer_insert=True,
		)
		_refuse(user, _("DialogBrain sign-in failed unexpectedly. See the Error Log."))
	finally:
		frappe.clear_messages()


def pop_result(user: str) -> "dict | None":
	"""The last OAuth outcome for ``user`` (read once): ``{ok, message, workspace?, …}``."""
	cache = frappe.cache()
	key = _result_key(user)
	result = cache.get_value(key, expires=True)
	if result is not None:
		cache.delete_value(key)
	return result
