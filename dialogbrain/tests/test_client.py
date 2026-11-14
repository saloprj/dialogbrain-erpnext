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

	def test_a_persons_calls_say_as_operator_and_nothing_else_does(self):
		"""DialogBrain treats a REST call as an automation unless it says ``as_operator``:
		a manager's reply and the take-over switch are a person's, a sync read is not."""
		ok = make_response(200, {"success": True, "data": {}})
		for tool, flagged in (
			("messages.send", True),
			("threads.set_agent_paused", True),
			("threads.get_agent_status", False),
			("search.threads", False),
			("agents.approve_draft", False),
		):
			with patch("requests.Session.request", return_value=ok) as req:
				client().execute(tool, {})
			self.assertEqual(req.call_args[1]["json"].get("as_operator"), True if flagged else None, tool)

	def test_execute_passes_thread_id(self):
		with patch("requests.Session.request", return_value=make_response(200, {"success": True, "data": []})) as req:
			client().execute("messages.send", {"text": "x"}, thread_id="42")
		self.assertEqual(req.call_args[1]["json"]["thread_id"], "42")

	def test_execute_sends_request_id_and_records_the_echo(self):
		body = {"success": True, "data": {"id": 1}, "request_id": "erp-abc"}
		with patch("requests.Session.request", return_value=make_response(200, body)) as req:
			c = client()
			data = c.execute("workspace.current", {}, request_id="erp-abc")
		self.assertEqual(data, {"id": 1})
		self.assertEqual(req.call_args[1]["json"]["request_id"], "erp-abc")
		self.assertEqual(c.last_request_id, "erp-abc")

	def test_execute_without_request_id_sends_none_and_old_servers_echo_nothing(self):
		with patch("requests.Session.request", return_value=make_response(200, {"success": True, "data": 1})) as req:
			c = client()
			c.execute("workspace.current", {})
		self.assertNotIn("request_id", req.call_args[1]["json"])
		self.assertIsNone(c.last_request_id)

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

	def test_execute_error_exposes_error_type(self):
		body = {"success": False, "error": "needs the admin role", "error_type": "permission_denied"}
		with patch("requests.Session.request", return_value=make_response(200, body)):
			with self.assertRaises(DialogBrainError) as cm:
				client().execute("integrations.set_thread_context", {})
		self.assertEqual(cm.exception.error_type, "permission_denied")

	def test_execute_error_without_error_type_is_none(self):
		body = {"success": False, "error": "Thread not found"}
		with patch("requests.Session.request", return_value=make_response(200, body)):
			with self.assertRaises(DialogBrainError) as cm:
				client().execute("messages.send", {})
		self.assertIsNone(cm.exception.error_type)
		self.assertIsNone(DialogBrainError("x").error_type)

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
		self.assertEqual(mask("db_live_abcdef1234"), "db_…1234")
		self.assertEqual(mask("db_test_abcdef1234"), "db_…1234")

	def test_read_timeout_is_dialogbrain_timeout(self):
		import requests

		from dialogbrain.client import DialogBrainTimeout

		with patch("requests.Session.request", side_effect=requests.ReadTimeout("slow db_live_x")):
			with self.assertRaises(DialogBrainTimeout) as ctx:
				client().execute("messages.send", {"thread_id": "1", "text": "x"})
		self.assertIsInstance(ctx.exception, DialogBrainError)
		self.assertNotIn("db_live_x", str(ctx.exception))

	def test_connect_timeout_is_not_ambiguous(self):
		# the request never reached the server: a plain error, safe to retry
		import requests

		from dialogbrain.client import DialogBrainTimeout

		with patch("requests.Session.request", side_effect=requests.ConnectTimeout("no route")):
			with self.assertRaises(DialogBrainError) as ctx:
				client().execute("messages.send", {"thread_id": "1", "text": "x"})
		self.assertNotIsInstance(ctx.exception, DialogBrainTimeout)

	def test_upload_file_three_steps(self):
		import hashlib

		calls = []

		def execute(tool_id, params, thread_id=None):
			calls.append((tool_id, params))
			if tool_id == "files.create_upload_url":
				return {"file_id": 77, "upload_url": "https://s3.test/put", "method": "PUT", "headers": {"Content-Type": "text/plain"}}
			return {"file_id": 77, "status": "ready"}

		c = client()
		with patch.object(c, "execute", side_effect=execute), patch(
			"requests.request", return_value=make_response(200, None)
		) as put:
			file_id = c.upload_file("a.txt", b"hello", "text/plain")
		self.assertEqual(file_id, 77)
		self.assertEqual(calls[0][0], "files.create_upload_url")
		self.assertEqual(
			calls[0][1],
			{"filename": "a.txt", "mime_type": "text/plain", "size_bytes": 5, "sha256": hashlib.sha256(b"hello").hexdigest()},
		)
		args, kwargs = put.call_args
		self.assertEqual(args[:2], ("PUT", "https://s3.test/put"))
		self.assertEqual(kwargs["data"], b"hello")
		self.assertEqual(kwargs["headers"], {"Content-Type": "text/plain"})
		self.assertNotIn("Authorization", kwargs["headers"])
		self.assertEqual(calls[1], ("files.complete_upload", {"file_id": 77}))

	def test_upload_file_deduplicated_skips_put(self):
		c = client()
		with patch.object(c, "execute", return_value={"file_id": 5, "deduplicated": True}) as ex, patch(
			"requests.request"
		) as put:
			self.assertEqual(c.upload_file("a.txt", b"x", "text/plain"), 5)
		put.assert_not_called()
		self.assertEqual(ex.call_count, 1)

	def test_upload_file_put_failure_raises(self):
		c = client()
		with patch.object(c, "execute", return_value={"file_id": 5, "upload_url": "https://s3.test/put"}), patch(
			"requests.request", return_value=make_response(403, None, text="denied")
		):
			with self.assertRaises(DialogBrainError):
				c.upload_file("a.txt", b"x", "text/plain")
