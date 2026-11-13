"""OAuth 2.1 (authorization code + PKCE, public client) helpers for connecting to DialogBrain.

Pure protocol functions: no Frappe document access. Secrets (authorization code, PKCE
verifier, returned API key) never appear in error messages.
"""

import base64
import hashlib
import re
import secrets
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from dialogbrain.client import DialogBrainError

TIMEOUT = 20
KEY_PREFIX = "db_"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


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
	return meta


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
