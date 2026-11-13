from unittest.mock import MagicMock, patch

from dialogbrain.client import DialogBrainClient, DialogBrainError, mask
from dialogbrain.tests.utils import DialogBrainTestCase, load_fixture

KEY = "db_live_x"
BASE = "http://dialogbrain.test"


def make_response(status=200, body=None, text=""):
	resp = MagicMock()
	resp.status_code = status
	resp.ok = status < 400
	resp.text = text
	if body is None:
		resp.json.side_effect = ValueError("no json")
	else:
		resp.json.return_value = body
	return resp


def client():
	return DialogBrainClient(BASE, KEY)


class TestClient(DialogBrainTestCase):
	def test_execute_posts_tool_body(self):
		with patch("requests.Session.request", return_value=make_response(200, {"success": True, "data": {}})) as req:
			client().execute("search.threads", {"limit": 5})
		args, kwargs = req.call_args
		self.assertEqual(args[0], "POST")
		self.assertEqual(args[1], BASE + "/api/tools/search.threads/execute")
		self.assertEqual(
			kwargs["json"],
			{"tool_id": "search.threads", "params": {"limit": 5}, "mode": "auto", "skip_approval": True},
		)
		self.assertEqual(kwargs["headers"]["Authorization"], "Bearer db_live_x")

	def test_execute_passes_thread_id(self):
		with patch("requests.Session.request", return_value=make_response(200, {"success": True, "data": []})) as req:
			client().execute("messages.send", {"text": "x"}, thread_id="42")
		self.assertEqual(req.call_args[1]["json"]["thread_id"], "42")

	def test_execute_returns_data(self):
		body = load_fixture("search_threads")
		with patch("requests.Session.request", return_value=make_response(200, body)):
			data = client().execute("search.threads", {})
		self.assertEqual(data, body["data"])

	def test_execute_raises_on_success_false(self):
		body = {"success": False, "data": None, "error": "Thread not found"}
		with patch("requests.Session.request", return_value=make_response(200, body)):
			with self.assertRaises(DialogBrainError) as cm:
				client().execute("messages.send", {})
		self.assertEqual(cm.exception.code, "Thread not found")
		self.assertIsNone(cm.exception.status)

	def test_execute_raises_on_http_401(self):
		with patch("requests.Session.request", return_value=make_response(401, {"detail": {"code": "INVALID_API_KEY", "message": "Invalid API key"}})):
			with self.assertRaises(DialogBrainError) as cm:
				client().execute("search.threads", {})
		self.assertEqual(cm.exception.status, 401)
		self.assertEqual(cm.exception.code, "INVALID_API_KEY")

	def test_error_message_never_contains_key(self):
		resp = make_response(500, None, text="boom Bearer db_live_x leaked db_live_x")
		with patch("requests.Session.request", return_value=resp):
			with self.assertRaises(DialogBrainError) as cm:
				client().execute("search.threads", {})
		self.assertNotIn("db_live_x", str(cm.exception))
		self.assertNotIn("db_live_x", repr(cm.exception))

	def test_network_error_wrapped_without_key(self):
		import requests

		with patch("requests.Session.request", side_effect=requests.ConnectionError("fail db_live_x")):
			with self.assertRaises(DialogBrainError) as cm:
				client().execute("search.threads", {})
		self.assertNotIn("db_live_x", str(cm.exception))

	def test_requires_approval_raises(self):
		body = {"success": False, "requires_approval": True, "message": "needs approval"}
		with patch("requests.Session.request", return_value=make_response(200, body)):
			with self.assertRaises(DialogBrainError) as cm:
				client().execute("messages.send", {})
		self.assertEqual(cm.exception.code, "requires_approval")

	def test_list_tools(self):
		body = load_fixture("tools_catalog_trimmed")
		with patch("requests.Session.request", return_value=make_response(200, body)) as req:
			tools = client().list_tools()
		self.assertEqual(req.call_args[0][:2], ("GET", BASE + "/api/tools/catalog"))
		self.assertIn("messages.send", tools)
		self.assertIsInstance(tools, set)

	def test_create_webhook(self):
		body = load_fixture("webhooks_create")
		with patch("requests.Session.request", return_value=make_response(200, body)) as req:
			res = client().create_webhook("https://x/y", ["message.received"])
		self.assertEqual(req.call_args[0][:2], ("POST", BASE + "/api/v1/webhooks"))
		self.assertEqual(req.call_args[1]["json"], {"url": "https://x/y", "event_types": ["message.received"]})
		self.assertEqual(res["id"], 9)
		self.assertTrue(res["secret"].startswith("whsec_"))

	def test_delete_activate_list_deliveries(self):
		c = client()
		with patch("requests.Session.request", return_value=make_response(204, None)) as req:
			self.assertIsNone(c.delete_webhook("9"))
		self.assertEqual(req.call_args[0][:2], ("DELETE", BASE + "/api/v1/webhooks/9"))
		with patch("requests.Session.request", return_value=make_response(200, {"status": "activated"})) as req:
			self.assertIsNone(c.activate_webhook("9"))
		self.assertEqual(req.call_args[0][:2], ("PATCH", BASE + "/api/v1/webhooks/9/activate"))
		with patch("requests.Session.request", return_value=make_response(200, load_fixture("webhooks_list"))) as req:
			self.assertEqual(c.list_webhooks()[0]["id"], 9)
		with patch("requests.Session.request", return_value=make_response(200, load_fixture("webhooks_deliveries"))) as req:
			self.assertEqual(len(c.webhook_deliveries("9", limit=2)), 2)
		self.assertEqual(req.call_args[1]["params"], {"limit": 2})

	def test_mask(self):
		self.assertEqual(mask("db_live_abcdef1234"), "db_live_…1234")
