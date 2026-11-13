import base64
import hashlib
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import requests as real_requests

from dialogbrain.client import DialogBrainError
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
