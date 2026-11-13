import base64
import hashlib
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import frappe
import requests as real_requests

from dialogbrain.client import DialogBrainError
from dialogbrain.setup import connect as connect_mod
from dialogbrain.setup import oauth
from dialogbrain.tests.utils import DialogBrainTestCase

CB = "https://erp.example.com/api/method/dialogbrain.setup.oauth.callback"


def resp(status=200, body=None, text=""):
	r = MagicMock()
	r.status_code = status
	r.text = text
	if body is None:
		r.json.side_effect = ValueError("no json")
	else:
		r.json.return_value = body
	return r


def mock_requests(**kw):
	m = MagicMock()
	m.RequestException = real_requests.RequestException
	for k, v in kw.items():
		setattr(m, k, v)
	return m


META = {
	"issuer": "https://api.dialogbrain.test",
	"authorization_endpoint": "https://app.dialogbrain.test/oauth/authorize",
	"token_endpoint": "https://api.dialogbrain.test/oauth/token",
	"registration_endpoint": "https://api.dialogbrain.test/oauth/register",
	"code_challenge_methods_supported": ["S256"],
}


class TestOAuthProtocol(DialogBrainTestCase):
	def test_discover_returns_metadata(self):
		m = mock_requests(get=MagicMock(return_value=resp(200, META)))
		with patch("dialogbrain.setup.oauth.requests", m):
			self.assertEqual(oauth.discover("https://api.dialogbrain.test/"), META)
		args, kwargs = m.get.call_args
		self.assertEqual(args[0], "https://api.dialogbrain.test/.well-known/oauth-authorization-server")
		self.assertEqual(kwargs["timeout"], 20)

	def test_discover_rejects_missing_s256(self):
		meta = dict(META, code_challenge_methods_supported=["plain"])
		m = mock_requests(get=MagicMock(return_value=resp(200, meta)))
		with patch("dialogbrain.setup.oauth.requests", m):
			with self.assertRaises(DialogBrainError):
				oauth.discover("https://api.dialogbrain.test")

	def test_discover_rejects_missing_endpoint_and_non_200(self):
		meta = {k: v for k, v in META.items() if k != "registration_endpoint"}
		for r in (resp(200, meta), resp(404, None, "nope")):
			m = mock_requests(get=MagicMock(return_value=r))
			with patch("dialogbrain.setup.oauth.requests", m):
				with self.assertRaises(DialogBrainError):
					oauth.discover("https://api.dialogbrain.test")

	def test_discover_unreachable_raises(self):
		m = mock_requests(get=MagicMock(side_effect=real_requests.ConnectionError("down")))
		with patch("dialogbrain.setup.oauth.requests", m):
			with self.assertRaises(DialogBrainError):
				oauth.discover("https://api.dialogbrain.test")

	def test_register_client_posts_redirect_and_name(self):
		m = mock_requests(post=MagicMock(return_value=resp(201, {"client_id": "cid-1"})))
		with patch("dialogbrain.setup.oauth.requests", m):
			cid = oauth.register_client(META["registration_endpoint"], CB, "ERPNext — erp.example.com")
		self.assertEqual(cid, "cid-1")
		args, kwargs = m.post.call_args
		self.assertEqual(args[0], META["registration_endpoint"])
		self.assertEqual(
			kwargs["json"], {"redirect_uris": [CB], "client_name": "ERPNext — erp.example.com"}
		)
		self.assertEqual(kwargs["timeout"], 20)

	def test_register_client_failure_raises(self):
		m = mock_requests(post=MagicMock(return_value=resp(400, None, "bad")))
		with patch("dialogbrain.setup.oauth.requests", m):
			with self.assertRaises(DialogBrainError):
				oauth.register_client(META["registration_endpoint"], CB, "x")

	def test_pkce_pair_matches_rfc7636(self):
		v, c = oauth.pkce_pair()
		self.assertTrue(43 <= len(v) <= 128)
		expected = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
		self.assertEqual(c, expected)
		self.assertNotEqual(v, oauth.pkce_pair()[0])

	def test_authorize_url_has_all_params(self):
		url = oauth.authorize_url(META["authorization_endpoint"], "cid", CB, "st", "chal")
		q = parse_qs(urlparse(url).query)
		self.assertTrue(url.startswith(META["authorization_endpoint"] + "?"))
		self.assertEqual(q["response_type"], ["code"])
		self.assertEqual(q["client_id"], ["cid"])
		self.assertEqual(q["redirect_uri"], [CB])
		self.assertEqual(q["state"], ["st"])
		self.assertEqual(q["code_challenge"], ["chal"])
		self.assertEqual(q["code_challenge_method"], ["S256"])
		self.assertEqual(q["scope"], ["mcp"])

	def test_authorize_url_keeps_existing_query(self):
		url = oauth.authorize_url("https://a.test/authorize?x=1", "cid", CB, "st", "chal")
		q = parse_qs(urlparse(url).query)
		self.assertEqual(q["x"], ["1"])
		self.assertEqual(q["state"], ["st"])

	def test_exchange_code_returns_key(self):
		m = mock_requests(
			post=MagicMock(return_value=resp(200, {"access_token": "db_live_abc123", "token_type": "bearer"}))
		)
		with patch("dialogbrain.setup.oauth.requests", m):
			key = oauth.exchange_code(META["token_endpoint"], "CODE", "VERIF", "cid", CB)
		self.assertEqual(key, "db_live_abc123")
		args, kwargs = m.post.call_args
		self.assertEqual(
			kwargs["data"],
			{
				"grant_type": "authorization_code",
				"code": "CODE",
				"redirect_uri": CB,
				"client_id": "cid",
				"code_verifier": "VERIF",
			},
		)
		self.assertEqual(kwargs["timeout"], 20)

	def test_exchange_code_error_hides_secrets(self):
		body = "bad code SECRETCODE verifier SECRETVERIF token db_live_SECRETTOKEN"
		m = mock_requests(post=MagicMock(return_value=resp(400, {"error": body}, body)))
		with patch("dialogbrain.setup.oauth.requests", m):
			with self.assertRaises(DialogBrainError) as cm:
				oauth.exchange_code(META["token_endpoint"], "SECRETCODE", "SECRETVERIF", "cid", CB)
		msg = str(cm.exception)
		for secret in ("SECRETCODE", "SECRETVERIF", "db_live_SECRETTOKEN", "SECRETTOKEN"):
			self.assertNotIn(secret, msg)

	def test_exchange_code_network_error_hides_secrets(self):
		m = mock_requests(post=MagicMock(side_effect=real_requests.ConnectionError("boom SECRETCODE SECRETVERIF")))
		with patch("dialogbrain.setup.oauth.requests", m):
			with self.assertRaises(DialogBrainError) as cm:
				oauth.exchange_code(META["token_endpoint"], "SECRETCODE", "SECRETVERIF", "cid", CB)
		self.assertNotIn("SECRETCODE", str(cm.exception))
		self.assertNotIn("SECRETVERIF", str(cm.exception))

	def test_exchange_code_rejects_bad_token(self):
		for body in ({}, {"access_token": "sk_other_123"}):
			m = mock_requests(post=MagicMock(return_value=resp(200, body)))
			with patch("dialogbrain.setup.oauth.requests", m):
				with self.assertRaises(DialogBrainError) as cm:
					oauth.exchange_code(META["token_endpoint"], "C", "V", "cid", CB)
			self.assertNotIn("sk_other_123", str(cm.exception))

	def test_redirect_allowed(self):
		for uri, ok in (
			("https://erp.example.com/x", True),
			("http://localhost:8080/x", True),
			("http://127.0.0.1/x", True),
			("http://[::1]:8000/x", True),
			("http://erp.local/x", False),
			("ftp://x", False),
		):
			self.assertEqual(oauth.redirect_allowed(uri), ok, uri)


# --- start / callback ---------------------------------------------------------------

SETTINGS = "DialogBrain Settings"
SITE = "https://erp.example.com"
KEY = "db_live_NEWKEYfromOAUTH0000"
OLD_KEY = "db_live_OLDKEY000000000000"
CODE = "AUTHCODE-xyz-123456"
OK_RES = {"workspace": "Test User", "channels": 8, "network_mode": "public", "warning": None}


def _user(email, *roles):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	user = frappe.get_doc(
		{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
	)
	user.insert(ignore_permissions=True)
	if roles:
		user.add_roles(*roles)
	return email


def _fake_get_url(site=SITE):
	def get_url(uri=None, *args, **kwargs):
		return site + (uri or "")

	return get_url


class TestOAuthFlow(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.local.flags.commit = False
		self.admin = _user("oauth-admin@example.com", "System Manager")
		self.other = _user("oauth-other@example.com", "System Manager")
		self.agent = _user("oauth-agent@example.com", "Chat Agent")
		doc = frappe.get_single(SETTINGS)
		doc.update(
			{
				"base_url": "https://api.dialogbrain.test",
				"api_key": OLD_KEY,
				"network_mode": "public",
				"public_site_url": SITE + "/",
				"oauth_client_id": None,
				"oauth_redirect_uri": None,
				"oauth_base_url": None,
				"connected": 0,
			}
		)
		doc.flags.dialogbrain_server_write = True
		doc.save()
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		frappe.set_user(self.admin)
		oauth.pop_result(self.admin)
		oauth.pop_result(self.other)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.response = frappe._dict()
		frappe.local.flags.commit = False

	# --- helpers ------------------------------------------------------------------

	def _get(self, field):
		return frappe.db.get_single_value(SETTINGS, field)

	def _start(self, site=SITE, meta=None, client_id="cid-1"):
		with patch("frappe.utils.get_url", _fake_get_url(site)), patch.object(
			oauth, "discover", return_value=dict(meta or META)
		) as disc, patch.object(oauth, "register_client", return_value=client_id) as reg:
			out = oauth.start()
		return out, disc, reg

	def _state(self, out):
		return parse_qs(urlparse(out["authorize_url"]).query)["state"][0]

	def _entry(self, state):
		return frappe.cache().get_value(f"dialogbrain:oauth:{state}", expires=True)

	def _callback(self, connect_result=OK_RES, connect_error=None, exchange_error=None, workspace=None, **params):
		frappe.local.response = frappe._dict()
		ex = MagicMock(return_value=KEY, side_effect=exchange_error)
		conn = MagicMock(return_value=connect_result, side_effect=connect_error)
		self.client_cls = MagicMock()
		self.client_cls.return_value.execute.return_value = workspace or {"id": 128, "name": "Test User"}
		with patch("frappe.utils.get_url", _fake_get_url()), patch.object(oauth, "exchange_code", ex), patch.object(
			connect_mod, "_connect", conn
		), patch.object(oauth, "DialogBrainClient", self.client_cls, create=True):
			oauth.callback(**params)
		return frappe.local.response, ex, conn

	def _mark_connected(self, workspace_id="128", workspace_name="Test User"):
		frappe.db.set_single_value(
			SETTINGS,
			{"connected": 1, "workspace_id": workspace_id, "workspace_name": workspace_name, "webhook_id": "wh-1"},
		)
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def _ok_params(self, state, **kw):
		return dict({"code": CODE, "state": state, "iss": META["issuer"]}, **kw)

	# --- start ----------------------------------------------------------------------

	def test_start_registers_client_and_caches_state(self):
		out, disc, reg = self._start()
		self.assertTrue(out["authorize_url"].startswith(META["authorization_endpoint"] + "?"))
		state = self._state(out)
		disc.assert_called_once_with("https://api.dialogbrain.test")
		reg.assert_called_once_with(META["registration_endpoint"], CB, "ERPNext — erp.example.com")
		entry = self._entry(state)
		self.assertEqual(entry["user"], self.admin)
		self.assertEqual(entry["client_id"], "cid-1")
		self.assertEqual(entry["redirect_uri"], CB)
		self.assertEqual(entry["token_endpoint"], META["token_endpoint"])
		self.assertEqual(entry["issuer"], META["issuer"])
		q = parse_qs(urlparse(out["authorize_url"]).query)
		expected = base64.urlsafe_b64encode(hashlib.sha256(entry["verifier"].encode()).digest()).rstrip(b"=")
		self.assertEqual(q["code_challenge"], [expected.decode()])
		self.assertNotIn(entry["verifier"], out["authorize_url"])
		self.assertEqual(self._get("oauth_client_id"), "cid-1")
		self.assertEqual(self._get("oauth_redirect_uri"), CB)

	def test_start_reuses_client_for_same_redirect(self):
		self._start()
		out, _, reg = self._start(client_id="cid-2")
		reg.assert_not_called()
		self.assertEqual(parse_qs(urlparse(out["authorize_url"]).query)["client_id"], ["cid-1"])
		self.assertEqual(self._get("oauth_client_id"), "cid-1")

	def test_start_reregisters_when_site_url_changes(self):
		self._start()
		out, _, reg = self._start(site="https://new.example.com", client_id="cid-2")
		new_cb = "https://new.example.com" + oauth.CALLBACK_PATH
		reg.assert_called_once_with(META["registration_endpoint"], new_cb, "ERPNext — new.example.com")
		self.assertEqual(parse_qs(urlparse(out["authorize_url"]).query)["client_id"], ["cid-2"])
		self.assertEqual(self._get("oauth_client_id"), "cid-2")
		self.assertEqual(self._get("oauth_redirect_uri"), new_cb)

	def test_start_reregisters_when_base_url_changes(self):
		self._start()
		frappe.db.set_single_value(SETTINGS, "base_url", "https://other-dialogbrain.test")
		out, disc, reg = self._start(client_id="cid-2")
		disc.assert_called_once_with("https://other-dialogbrain.test")
		reg.assert_called_once_with(META["registration_endpoint"], CB, "ERPNext — erp.example.com")
		self.assertEqual(parse_qs(urlparse(out["authorize_url"]).query)["client_id"], ["cid-2"])
		self.assertEqual(self._get("oauth_client_id"), "cid-2")
		self.assertEqual(self._get("oauth_base_url"), "https://other-dialogbrain.test")
		self.assertIn("oauth_base_url", connect_mod.SERVER_FIELDS)

	def test_start_discovery_failure_is_a_clean_error(self):
		err = DialogBrainError("Cannot reach DialogBrain at https://api.dialogbrain.test: ConnectionError")
		with patch("frappe.utils.get_url", _fake_get_url()), patch.object(
			oauth, "discover", side_effect=err
		), patch.object(oauth, "register_client") as reg, self.assertRaises(frappe.ValidationError) as cm:
			oauth.start()
		self.assertIn("Cannot reach DialogBrain", str(cm.exception))
		reg.assert_not_called()

	def test_start_registration_failure_is_a_clean_error(self):
		err = DialogBrainError("DialogBrain client registration failed (HTTP 400)", status=400)
		with patch("frappe.utils.get_url", _fake_get_url()), patch.object(
			oauth, "discover", return_value=dict(META)
		), patch.object(oauth, "register_client", side_effect=err), self.assertRaises(
			frappe.ValidationError
		) as cm:
			oauth.start()
		self.assertIn("registration failed", str(cm.exception))
		self.assertFalse(self._get("oauth_client_id"))

	def test_start_without_issuer_is_a_clean_error(self):
		meta = {k: v for k, v in META.items() if k != "issuer"}
		with self.assertRaises(frappe.ValidationError) as cm:
			self._start(meta=meta)
		self.assertIn("issuer", str(cm.exception))

	def test_start_refuses_plain_http_non_loopback(self):
		with self.assertRaises(frappe.ValidationError) as cm:
			self._start(site="http://erp.local")
		self.assertIn("Connect with API key", str(cm.exception))
		self.assertFalse(self._get("oauth_client_id"))

	def test_start_refuses_plain_http_before_any_request(self):
		with patch("frappe.utils.get_url", _fake_get_url("http://erp.local")), patch.object(
			oauth, "discover"
		) as disc, patch.object(oauth, "register_client") as reg, self.assertRaises(frappe.ValidationError):
			oauth.start()
		disc.assert_not_called()
		reg.assert_not_called()

	def test_start_requires_base_url(self):
		frappe.db.set_single_value(SETTINGS, "base_url", None)
		with patch.object(oauth, "discover") as disc, self.assertRaises(frappe.ValidationError):
			oauth.start()
		disc.assert_not_called()

	def test_start_requires_system_manager(self):
		frappe.set_user(self.agent)
		with self.assertRaises(frappe.PermissionError):
			self._start()
		self.assertIn(oauth.start, frappe.whitelisted)
		self.assertIn(oauth.callback, frappe.whitelisted)
		self.assertIn(oauth.callback, frappe.guest_methods)

	# --- callback -------------------------------------------------------------------

	def test_callback_happy_path_stores_key_and_connects(self):
		state = self._state(self._start()[0])
		response, ex, conn = self._callback(**self._ok_params(state))
		self.assertEqual(response["type"], "redirect")
		self.assertEqual(response["location"], "/app/dialogbrain-settings")
		entry_args = ex.call_args.args
		self.assertEqual(entry_args[0], META["token_endpoint"])
		self.assertEqual(entry_args[1], CODE)
		self.assertEqual(entry_args[3:], ("cid-1", CB))
		conn.assert_called_once()
		self.assertEqual(frappe.get_single(SETTINGS).get_password("api_key"), KEY)
		result = oauth.pop_result(self.admin)
		self.assertTrue(result["ok"])
		self.assertEqual(result["workspace"], "Test User")
		self.assertEqual(result["channels"], 8)
		self.assertIsNone(oauth.pop_result(self.admin))  # popped once

	def test_callback_unknown_state_refused(self):
		self._start()
		for state in ("forged-state-0123456789abcdef", "x", None, ""):
			response, ex, conn = self._callback(**self._ok_params(state))
			self.assertEqual(response["location"], "/app/dialogbrain-settings")
			self.assertFalse(oauth.pop_result(self.admin)["ok"])
			ex.assert_not_called()
			conn.assert_not_called()
		self.assertEqual(frappe.get_single(SETTINGS).get_password("api_key"), OLD_KEY)

	def test_callback_state_is_single_use(self):
		state = self._state(self._start()[0])
		_, ex1, _ = self._callback(**self._ok_params(state))
		self.assertTrue(oauth.pop_result(self.admin)["ok"])
		_, ex2, conn2 = self._callback(**self._ok_params(state, code="ATTACKER-CODE"))
		self.assertFalse(oauth.pop_result(self.admin)["ok"])
		self.assertEqual(ex1.call_count + ex2.call_count, 1)
		conn2.assert_not_called()

	def test_callback_other_user_refused(self):
		state = self._state(self._start()[0])
		frappe.set_user(self.other)
		response, ex, conn = self._callback(**self._ok_params(state))
		self.assertEqual(response["location"], "/app/dialogbrain-settings")
		ex.assert_not_called()
		conn.assert_not_called()
		self.assertFalse(oauth.pop_result(self.other)["ok"])
		self.assertEqual(frappe.get_single(SETTINGS).get_password("api_key"), OLD_KEY)

	def test_callback_iss_mismatch_refused(self):
		state = self._state(self._start()[0])
		_, ex, conn = self._callback(**self._ok_params(state, iss="https://evil.example.com"))
		ex.assert_not_called()
		conn.assert_not_called()
		self.assertFalse(oauth.pop_result(self.admin)["ok"])
		self.assertEqual(frappe.get_single(SETTINGS).get_password("api_key"), OLD_KEY)

	def test_callback_missing_iss_refused(self):
		state = self._state(self._start()[0])
		_, ex, _ = self._callback(code=CODE, state=state)
		ex.assert_not_called()
		self.assertFalse(oauth.pop_result(self.admin)["ok"])

	def test_callback_issuer_override_from_site_config(self):
		with patch.dict(frappe.local.conf, {"dialogbrain_oauth_issuer": "http://localhost:8000"}):
			state = self._state(self._start()[0])
			state2 = self._state(self._start()[0])
		self.assertEqual(self._entry(state)["issuer"], "http://localhost:8000")
		_, ex, _ = self._callback(**self._ok_params(state))  # discovery issuer: refused
		ex.assert_not_called()
		self.assertFalse(oauth.pop_result(self.admin)["ok"])
		_, ex, _ = self._callback(**self._ok_params(state2, iss="http://localhost:8000"))
		ex.assert_called_once()
		self.assertTrue(oauth.pop_result(self.admin)["ok"])

	def test_callback_guest_redirects_to_login_keeping_state(self):
		state = self._state(self._start()[0])
		full_path = f"{oauth.CALLBACK_PATH}?code={CODE}&state={state}&iss=x"
		frappe.set_user("Guest")
		req = MagicMock(full_path=full_path)
		with patch.object(frappe.local, "request", req, create=True):
			response, ex, conn = self._callback(**self._ok_params(state))
		self.assertEqual(response["type"], "redirect")
		self.assertTrue(response["location"].startswith("/login?redirect-to="))
		loc_q = parse_qs(urlparse(response["location"]).query)
		self.assertEqual(loc_q["redirect-to"], [full_path])
		self.assertIn(oauth.CALLBACK_PATH, loc_q["redirect-to"][0])
		ex.assert_not_called()
		conn.assert_not_called()
		raw = frappe.cache().make_key(f"dialogbrain:oauth:{state}")
		self.assertIsNotNone(frappe.cache().get(raw))  # still in Redis, unclaimed
		# after login the same state still completes
		frappe.set_user(self.admin)
		_, ex, _ = self._callback(**self._ok_params(state))
		ex.assert_called_once()
		self.assertTrue(oauth.pop_result(self.admin)["ok"])

	def test_callback_non_manager_redirects_with_error(self):
		state = self._state(self._start()[0])
		frappe.cache().set_value(
			f"dialogbrain:oauth:{state}", dict(self._entry(state), user=self.agent), expires_in_sec=60
		)
		frappe.set_user(self.agent)
		response, ex, conn = self._callback(**self._ok_params(state))  # no exception
		self.assertEqual(response["type"], "redirect")
		ex.assert_not_called()
		conn.assert_not_called()
		self.assertFalse(oauth.pop_result(self.agent)["ok"])

	def test_state_claim_is_atomic(self):
		state = self._state(self._start()[0])
		first = oauth._claim(state)
		second = oauth._claim(state)
		self.assertEqual(first["user"], self.admin)
		self.assertIsNone(second)

	def test_callback_access_denied_keeps_existing_key(self):
		state = self._state(self._start()[0])
		_, ex, conn = self._callback(state=state, error="access_denied", iss=META["issuer"])
		ex.assert_not_called()
		conn.assert_not_called()
		self.assertEqual(frappe.get_single(SETTINGS).get_password("api_key"), OLD_KEY)
		result = oauth.pop_result(self.admin)
		self.assertFalse(result["ok"])
		self.assertIn("access_denied", result["message"])
		# long error text is truncated
		state = self._state(self._start()[0])
		self._callback(state=state, error="x" * 500, iss=META["issuer"])
		self.assertLessEqual(oauth.pop_result(self.admin)["message"].count("x"), 100)

	def test_callback_exchange_failure_keeps_existing_key(self):
		state = self._state(self._start()[0])
		err = DialogBrainError("DialogBrain token exchange failed (HTTP 400): invalid_grant")
		_, _, conn = self._callback(exchange_error=err, **self._ok_params(state))
		conn.assert_not_called()
		self.assertEqual(frappe.get_single(SETTINGS).get_password("api_key"), OLD_KEY)
		result = oauth.pop_result(self.admin)
		self.assertFalse(result["ok"])
		self.assertIn("invalid_grant", result["message"])

	def test_callback_connect_failure_keeps_new_key(self):
		state = self._state(self._start()[0])
		err = frappe.ValidationError("This DialogBrain API key has no access to: messages.send")
		response, _, _ = self._callback(connect_error=err, **self._ok_params(state))
		self.assertEqual(response["location"], "/app/dialogbrain-settings")
		self.assertEqual(frappe.get_single(SETTINGS).get_password("api_key"), KEY)
		result = oauth.pop_result(self.admin)
		self.assertFalse(result["ok"])
		self.assertIn("messages.send", result["message"])

	# --- F1 (R4b): reconnect must not switch workspaces --------------------------------

	def test_callback_refuses_workspace_switch(self):
		self._mark_connected()
		state = self._state(self._start()[0])
		frappe.local.flags.commit = False
		_, ex, conn = self._callback(workspace={"id": 999, "name": "Other <b>WS</b>"}, **self._ok_params(state))
		ex.assert_called_once()
		self.client_cls.assert_called_once_with("https://api.dialogbrain.test", KEY)
		client = self.client_cls.return_value
		client.execute.assert_called_once_with("workspace.current", {})
		client.delete_webhook.assert_not_called()
		client.create_webhook.assert_not_called()
		client.list_webhooks.assert_not_called()
		conn.assert_not_called()
		self.assertEqual(frappe.get_single(SETTINGS).get_password("api_key"), OLD_KEY)
		self.assertEqual(self._get("workspace_id"), "128")
		self.assertEqual(self._get("webhook_id"), "wh-1")
		self.assertFalse(frappe.local.flags.commit)  # nothing saved
		result = oauth.pop_result(self.admin)
		self.assertFalse(result["ok"])
		self.assertEqual(
			result["message"],
			"This site is connected to Test User. Disconnect first to switch to Other <b>WS</b>.",
		)
		self.assertNotIn(KEY, str(result))

	def test_callback_same_workspace_reconnect_proceeds(self):
		self._mark_connected()
		state = self._state(self._start()[0])
		_, _, conn = self._callback(workspace={"id": 128, "name": "Test User"}, **self._ok_params(state))
		self.client_cls.return_value.execute.assert_called_once_with("workspace.current", {})
		conn.assert_called_once()
		self.assertEqual(frappe.get_single(SETTINGS).get_password("api_key"), KEY)
		self.assertTrue(oauth.pop_result(self.admin)["ok"])

	def test_callback_connect_failure_on_connected_site_restores_old_key(self):
		self._mark_connected()
		state = self._state(self._start()[0])
		err = frappe.ValidationError("This DialogBrain API key has no access to: messages.send")
		_, _, conn = self._callback(connect_error=err, **self._ok_params(state))
		conn.assert_called_once()
		self.assertEqual(frappe.get_single(SETTINGS).get_password("api_key"), OLD_KEY)
		self.assertEqual(self._get("connected"), 1)
		self.assertEqual(self._get("workspace_id"), "128")
		self.assertEqual(self._get("webhook_id"), "wh-1")
		result = oauth.pop_result(self.admin)
		self.assertFalse(result["ok"])
		self.assertIn("messages.send", result["message"])

	def test_callback_connect_failure_on_unconnected_site_keeps_new_key(self):
		state = self._state(self._start()[0])
		err = frappe.ValidationError("Invalid API key or DialogBrain unreachable")
		self._callback(connect_error=err, **self._ok_params(state))
		self.assertEqual(frappe.get_single(SETTINGS).get_password("api_key"), KEY)
		self.assertFalse(oauth.pop_result(self.admin)["ok"])

	def test_callback_unexpected_error_redirects_and_logs_deferred(self):
		state = self._state(self._start()[0])
		with patch("frappe.log_error") as log:
			response, _, conn = self._callback(exchange_error=RuntimeError(f"boom {CODE}"), **self._ok_params(state))
		self.assertEqual(response["type"], "redirect")
		self.assertEqual(response["location"], "/app/dialogbrain-settings")
		conn.assert_not_called()
		result = oauth.pop_result(self.admin)
		self.assertFalse(result["ok"])
		self.assertIn("Error Log", result["message"])
		log.assert_called_once()
		self.assertTrue(log.call_args.kwargs.get("defer_insert"))
		self.assertNotIn(CODE, str(log.call_args))
		self.assertEqual(frappe.get_single(SETTINGS).get_password("api_key"), OLD_KEY)

	def test_callback_sets_commit_flag(self):
		"""R3b: GET requests are rolled back unless flags.commit is set."""
		state = self._state(self._start()[0])
		frappe.local.flags.commit = False
		self._callback(**self._ok_params(state))
		self.assertTrue(frappe.local.flags.commit)
		self.assertTrue(oauth.pop_result(self.admin)["ok"])

		state = self._state(self._start()[0])
		frappe.local.flags.commit = False
		err = frappe.ValidationError("Invalid API key or DialogBrain unreachable")
		self._callback(connect_error=err, **self._ok_params(state))
		self.assertTrue(frappe.local.flags.commit)
		self.assertFalse(oauth.pop_result(self.admin)["ok"])

		state = self._state(self._start()[0])
		frappe.local.flags.commit = False
		self._callback(state=state, error="access_denied", iss=META["issuer"])
		self.assertFalse(frappe.local.flags.commit)  # nothing saved, nothing to commit

	def test_callback_sets_public_site_url_when_empty(self):
		frappe.db.set_single_value(SETTINGS, "public_site_url", None)
		state = self._state(self._start()[0])
		self._callback(**self._ok_params(state))
		self.assertEqual(self._get("public_site_url"), SITE)
		self.assertEqual(oauth.pop_result(self.admin)["public_site_url_filled"], SITE)

	def test_callback_keeps_public_site_url_when_set(self):
		state = self._state(self._start()[0])
		self._callback(**self._ok_params(state))
		self.assertEqual(self._get("public_site_url"), SITE + "/")
		self.assertNotIn("public_site_url_filled", oauth.pop_result(self.admin))

	def test_server_fields_protected(self):
		self._start()
		frappe.set_user("Administrator")
		doc = frappe.get_single(SETTINGS)
		doc.oauth_client_id = "attacker"
		doc.oauth_redirect_uri = "https://evil.example.com/cb"
		doc.backfill_days = 9
		doc.save()
		self.assertEqual(self._get("oauth_client_id"), "cid-1")
		self.assertEqual(self._get("oauth_redirect_uri"), CB)
		self.assertEqual(self._get("backfill_days"), 9)
		self.assertIn("oauth_client_id", connect_mod.SERVER_FIELDS)
		self.assertIn("oauth_redirect_uri", connect_mod.SERVER_FIELDS)

	def test_no_secret_in_redirect_or_result(self):
		state = self._state(self._start()[0])
		verifier = self._entry(state)["verifier"]
		response, _, _ = self._callback(**self._ok_params(state))
		result = oauth.pop_result(self.admin)
		state2 = self._state(self._start()[0])
		verifier2 = self._entry(state2)["verifier"]
		err = frappe.ValidationError(f"boom {KEY} {CODE} {verifier2}")
		response2, _, _ = self._callback(connect_error=err, **self._ok_params(state2))
		result2 = oauth.pop_result(self.admin)
		for blob in (str(response), str(result), str(response2), str(result2)):
			for secret in (KEY, CODE, verifier, verifier2):
				self.assertNotIn(secret, blob)

	def test_settings_onload_pops_result(self):
		state = self._state(self._start()[0])
		self._callback(**self._ok_params(state))
		doc = frappe.get_single(SETTINGS)
		doc.run_method("onload")
		self.assertTrue(doc.get_onload().get("oauth_result")["ok"])
		doc = frappe.get_single(SETTINGS)
		doc.run_method("onload")
		self.assertFalse(doc.get_onload().get("oauth_result"))
