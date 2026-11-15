import copy
from unittest.mock import MagicMock, patch

import frappe

from dialogbrain.client import DialogBrainError
from dialogbrain.tests.utils import DialogBrainTestCase, load_fixture

SETTINGS = "DialogBrain Settings"
SITE = "https://erp.example.com"
HOOK_URL = SITE + "/api/method/dialogbrain.api.webhook.receive"
SECRET = "whsec_" + "ab" * 32
EVENTS = [
	"message.received",
	"message.updated",
	"message.deleted",
	"conversation.started",
	"conversation.updated",
	"draft.created",
	"handoff.requested",
	"contact.updated",
]
REQUIRED = {
	"workspace.current",
	"channels.list",
	"search.threads",
	"messages.read_history",
	"messages.send",
	"files.create_upload_url",
	"files.complete_upload",
}


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


def _fake_client(tools=None, hooks=None, deliveries=None, workspace_error=None):
	client = MagicMock()
	workspace = load_fixture("workspace_current")["data"]
	channels = load_fixture("channels_list")["data"]

	def execute(tool_id, params, thread_id=None):
		if tool_id == "workspace.current":
			if workspace_error:
				raise workspace_error
			return copy.deepcopy(workspace)
		if tool_id == "channels.list":
			return copy.deepcopy(channels)
		raise AssertionError(f"unexpected tool {tool_id}")

	client.execute.side_effect = execute
	client.list_tools.return_value = set(REQUIRED if tools is None else tools) | {"agents.list"}
	client.list_webhooks.return_value = hooks if hooks is not None else []
	created = dict(load_fixture("webhooks_create"), id=41, url=HOOK_URL, secret=SECRET)
	client.create_webhook.return_value = created
	client.webhook_deliveries.return_value = deliveries if deliveries is not None else []
	return client


class TestConnect(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for dt in ("DB Message", "DB Conversation", "DB Channel User", "DB Channel"):
			frappe.db.delete(dt)
		doc = frappe.get_single(SETTINGS)
		doc.update(
			{
				"base_url": "https://api.example.com",
				"api_key": "db_live_testkey000000000000",
				"network_mode": "public",
				"public_site_url": SITE + "/",
				"workspace_id": None,
				"workspace_name": None,
				"webhook_id": None,
				"webhook_secret": None,
				"webhook_status": None,
				"connected": 0,
				"backfill_status": None,
				"reconcile_failures": None,
				"reconcile_status": None,
				"last_reconcile_at": None,
			}
		)
		doc.flags.dialogbrain_server_write = True
		doc.save()
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def tearDown(self):
		frappe.set_user("Administrator")

	# --- helpers ----------------------------------------------------------------

	def _get(self, field):
		return frappe.db.get_single_value(SETTINGS, field)

	def _call(self, fn, client, *args, **kwargs):
		from dialogbrain.setup import connect as mod

		with patch.object(mod, "DialogBrainClient", return_value=client), patch("frappe.enqueue") as enqueue:
			result = getattr(mod, fn)(*args, **kwargs)
		return result, enqueue

	def _connect(self, client=None, mode="public"):
		if mode != "public":
			frappe.db.set_single_value(SETTINGS, "network_mode", mode)
		client = client or _fake_client()
		result, enqueue = self._call("connect", client)
		return client, result, enqueue

	# --- connect ----------------------------------------------------------------

	def test_connect_happy_path_public(self):
		client, result, enqueue = self._connect()

		self.assertEqual(
			result, {"workspace": "Test User", "channels": 8, "network_mode": "public", "warning": None}
		)
		client.create_webhook.assert_called_once_with(HOOK_URL, EVENTS)
		self.assertEqual(self._get("webhook_id"), "41")
		secret = frappe.get_single(SETTINGS).get_password("webhook_secret")
		self.assertEqual(secret, SECRET)
		self.assertEqual(self._get("connected"), 1)
		self.assertEqual(self._get("workspace_id"), "128")
		self.assertEqual(self._get("workspace_name"), "Test User")
		self.assertIsNotNone(self._get("last_reconcile_at"))
		self.assertEqual(frappe.db.count("DB Channel"), 8)
		self.assertEqual(frappe.db.get_value("DB Channel", "155", "channel_type"), "telegram")
		# backfill through enqueue_backfill (dedup job id, queued status)
		self.assertEqual(self._get("backfill_status"), "queued")
		jobs = [c.args[0] for c in enqueue.call_args_list]
		self.assertIn("dialogbrain.sync.backfill.run_backfill", jobs)
		self.assertEqual(enqueue.call_args.kwargs.get("job_id"), "dialogbrain_backfill")

	def test_connect_private_creates_no_webhook(self):
		client, result, _ = self._connect(mode="private")

		self.assertEqual(result["network_mode"], "private")
		client.create_webhook.assert_not_called()
		self.assertFalse(self._get("webhook_id"))
		self.assertEqual(self._get("connected"), 1)
		self.assertEqual(self._get("backfill_status"), "queued")

	def test_connect_invalid_key_message(self):
		client = _fake_client(workspace_error=DialogBrainError("DialogBrain HTTP 401: Invalid API key", status=401))
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._connect(client)
		self.assertIn("Invalid API key or DialogBrain unreachable", str(ctx.exception))
		self.assertEqual(self._get("connected"), 0)
		client.create_webhook.assert_not_called()

	def test_connect_missing_tool_message(self):
		client = _fake_client(tools=REQUIRED - {"messages.send"})
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._connect(client)
		self.assertIn("messages.send", str(ctx.exception))
		self.assertEqual(self._get("connected"), 0)
		client.create_webhook.assert_not_called()

	def test_connect_webhooks_api_unavailable_message(self):
		client = _fake_client()
		client.list_webhooks.side_effect = DialogBrainError("DialogBrain HTTP 403: forbidden", status=403)
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._connect(client)
		self.assertIn("webhooks", str(ctx.exception))
		self.assertEqual(self._get("connected"), 0)

	def test_connect_replaces_existing_subscription_same_url(self):
		hooks = [
			dict(load_fixture("webhooks_list")[0], id=7, url=HOOK_URL),
			dict(load_fixture("webhooks_list")[0], id=8, url="https://other.example.com/hook"),
		]
		client, _, _ = self._connect(_fake_client(hooks=hooks))

		client.delete_webhook.assert_called_once_with(7)
		client.create_webhook.assert_called_once()
		self.assertEqual(self._get("webhook_id"), "41")

	def test_connect_requires_public_site_url_in_public_mode(self):
		frappe.db.set_single_value(SETTINGS, "public_site_url", None)
		client = _fake_client()
		with self.assertRaises(frappe.ValidationError):
			self._connect(client)
		client.create_webhook.assert_not_called()

	def test_connect_requires_system_manager(self):
		from dialogbrain.setup import connect as mod

		user = _user("connect-agent@example.com", "Chat Agent")
		client = _fake_client()
		frappe.set_user(user)
		try:
			for fn, args in (
				("connect", ()),
				("disconnect", ()),
				("resync", ()),
				("set_network_mode", ("private",)),
				("webhook_health", ()),
			):
				with self.subTest(fn=fn), self.assertRaises(frappe.PermissionError):
					self._call(fn, client, *args)
		finally:
			frappe.set_user("Administrator")
		client.create_webhook.assert_not_called()
		self.assertEqual(self._get("connected"), 0)
		# whitelisted (callable from the form)
		for fn in ("connect", "disconnect", "resync", "set_network_mode", "webhook_health"):
			self.assertIn(getattr(mod, fn), frappe.whitelisted)

	# --- disconnect / resync / mode -----------------------------------------------

	def test_disconnect_keeps_data(self):
		client, _, _ = self._connect()
		client.delete_webhook.side_effect = DialogBrainError("DialogBrain HTTP 404: not found", status=404)

		self._call("disconnect", client)

		client.delete_webhook.assert_called_once_with("41")
		self.assertEqual(self._get("connected"), 0)
		self.assertFalse(self._get("webhook_id"))
		self.assertEqual(frappe.db.count("DB Channel"), 8)  # mirrored data kept

	def test_resync_enqueues_backfill(self):
		client, _, _ = self._connect()
		frappe.db.set_single_value(SETTINGS, "backfill_status", "done: 3 threads")
		frappe.db.set_single_value(SETTINGS, "reconcile_failures", '{"x": {"count": 5}}')

		_, enqueue = self._call("resync", client)

		self.assertEqual(self._get("backfill_status"), "queued")
		self.assertFalse(self._get("reconcile_failures"))
		self.assertEqual(enqueue.call_args.kwargs.get("job_id"), "dialogbrain_backfill")

	def test_switch_to_private_deletes_webhook(self):
		client, _, _ = self._connect()

		self._call("set_network_mode", client, "private")

		client.delete_webhook.assert_called_once_with("41")
		self.assertEqual(self._get("network_mode"), "private")
		self.assertFalse(self._get("webhook_id"))
		self.assertFalse(frappe.get_single(SETTINGS).get_password("webhook_secret", raise_exception=False))
		self.assertEqual(self._get("connected"), 1)

	def test_switch_to_public_creates_webhook(self):
		client, _, _ = self._connect(mode="private")

		self._call("set_network_mode", client, "public")

		client.create_webhook.assert_called_once_with(HOOK_URL, EVENTS)
		self.assertEqual(self._get("network_mode"), "public")
		self.assertEqual(self._get("webhook_id"), "41")

	def test_set_network_mode_rejects_unknown_mode(self):
		with self.assertRaises(frappe.ValidationError):
			self._call("set_network_mode", _fake_client(), "tunnel")

	# --- webhook health -------------------------------------------------------------

	def test_webhook_health_paused(self):
		client, _, _ = self._connect()
		failed = {
			"id": 700,
			"event_type": "message.received",
			"status": "failed",
			"response_status": 500,
			"attempt": 5,
			"max_attempts": 5,
			"created_at": "2026-11-13T12:10:00+00:00",
			"delivered_at": None,
			"next_attempt_at": None,
		}
		deliveries = [dict(failed, id=703), dict(failed, id=702), dict(failed, id=701)]
		deliveries += load_fixture("webhooks_deliveries")
		client.list_webhooks.return_value = []  # a paused subscription is not listed
		client.webhook_deliveries.return_value = deliveries

		health, _ = self._call("webhook_health", client)

		client.webhook_deliveries.assert_called_with("41", limit=10)
		self.assertFalse(health["active"])
		self.assertEqual(health["failures"], 3)
		self.assertEqual(health["last_status"], "failed")
		self.assertEqual(health["last_delivery_at"], "2026-11-13T12:10:00+00:00")
		self.assertIn("paused", (self._get("webhook_status") or "").lower())

	def test_webhook_health_active(self):
		client, _, _ = self._connect()
		client.list_webhooks.return_value = [dict(load_fixture("webhooks_list")[0], id=41, url=HOOK_URL)]
		client.webhook_deliveries.return_value = load_fixture("webhooks_deliveries")

		health, _ = self._call("webhook_health", client)

		self.assertTrue(health["active"])
		self.assertEqual(health["failures"], 0)
		self.assertEqual(health["last_status"], "delivered")
		self.assertEqual(health["last_delivery_at"], "2026-11-13T12:07:24.028093+00:00")
		self.assertTrue((self._get("webhook_status") or "").startswith("active"))

	# --- R14: form saves cannot clobber server-managed fields ---------------------------

	def test_form_save_cannot_clobber_server_fields(self):
		self._connect()
		frappe.db.set_single_value(SETTINGS, "backfill_status", "running 3/10")
		frappe.db.set_single_value(SETTINGS, "reconcile_failures", '{"t1": {"count": 2}}')
		frappe.db.set_single_value(SETTINGS, "reconcile_status", "1 thread failing")
		before = frappe.db.get_value(
			SETTINGS,
			SETTINGS,
			["workspace_id", "workspace_name", "webhook_id", "webhook_status", "last_reconcile_at"],
			as_dict=True,
		)

		doc = frappe.get_single(SETTINGS)
		doc.update(
			{
				"workspace_id": "999",
				"workspace_name": "Hacked",
				"webhook_id": "1",
				"webhook_secret": "whsec_" + "00" * 32,
				"webhook_status": "x",
				"last_reconcile_at": "2020-01-01 00:00:00",
				"reconcile_failures": None,
				"reconcile_status": None,
				"backfill_status": "done",
				"connected": 0,
				"backfill_days": 7,  # a user field still saves
			}
		)
		doc.save()

		after = frappe.db.get_value(
			SETTINGS,
			SETTINGS,
			["workspace_id", "workspace_name", "webhook_id", "webhook_status", "last_reconcile_at"],
			as_dict=True,
		)
		self.assertEqual(after, before)
		self.assertEqual(self._get("connected"), 1)
		self.assertEqual(self._get("backfill_status"), "running 3/10")
		self.assertEqual(self._get("reconcile_failures"), '{"t1": {"count": 2}}')
		self.assertEqual(self._get("reconcile_status"), "1 thread failing")
		self.assertEqual(frappe.get_single(SETTINGS).get_password("webhook_secret"), SECRET)
		self.assertEqual(self._get("backfill_days"), 7)

	def test_form_save_changing_mode_reconfigures_webhook(self):
		client, _, _ = self._connect()
		doc = frappe.get_single(SETTINGS)
		doc.network_mode = "private"
		from dialogbrain.setup import connect as mod

		with patch.object(mod, "DialogBrainClient", return_value=client):
			doc.save()

		client.delete_webhook.assert_called_once_with("41")
		self.assertEqual(self._get("network_mode"), "private")
		self.assertFalse(self._get("webhook_id"))

	def test_background_status_writes_keep_form_timestamp(self):
		"""A settings form opened before a backfill/reconcile status write must still save."""
		from dialogbrain.sync.backfill import set_setting

		doc = frappe.get_single(SETTINGS)
		modified = frappe.db.get_single_value(SETTINGS, "modified")
		set_setting("backfill_status", "running 1/2")
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "modified"), modified)
		doc.backfill_days = 14
		doc.save()  # no TimestampMismatchError
		self.assertEqual(self._get("backfill_days"), 14)


class TestSchedulerTickWarning(DialogBrainTestCase):
	"""R15: private mode polls every minute only with a 60 s scheduler tick. The app does not
	edit site config; it tells the admin the exact command."""

	def setUp(self):
		TestConnect.setUp(self)

	def tearDown(self):
		frappe.set_user("Administrator")

	def _call(self, fn, client, *args):
		return TestConnect._call(self, fn, client, *args)

	def _get(self, field):
		return frappe.db.get_single_value(SETTINGS, field)

	def _tick(self, value, site_value=None):
		"""``value`` in common_site_config.json (what ``bench schedule`` reads at start);
		``site_value`` in the site config (which the scheduler never reads)."""
		common = {} if value is None else {"scheduler_tick_interval": value}
		conf = dict(frappe.local.conf)
		conf.pop("scheduler_tick_interval", None)
		if site_value is not None:
			conf["scheduler_tick_interval"] = site_value
		from contextlib import ExitStack

		stack = ExitStack()
		stack.enter_context(patch.object(frappe, "get_common_site_config", return_value=frappe._dict(common)))
		stack.enter_context(patch.object(frappe.local, "conf", frappe._dict(conf)))
		return stack

	def test_effective_tick_default_follows_frappe(self):
		from dialogbrain.setup.connect import scheduler_tick

		try:
			from frappe.utils.scheduler import DEFAULT_SCHEDULER_TICK as expected
		except ImportError:  # Frappe v15: the tick defaults to 60 s
			expected = 60
		with self._tick(None):
			self.assertEqual(scheduler_tick(), expected)
		with self._tick("90"):
			self.assertEqual(scheduler_tick(), 90)

	def test_warning_only_for_private_with_slow_tick(self):
		from dialogbrain.setup.connect import scheduler_warning

		with self._tick(240):
			text = scheduler_warning("private")
			self.assertIn("bench set-config -g scheduler_tick_interval 60", text)
			self.assertIn("restart", text)
			self.assertNotIn("--site", text)
			self.assertIsNone(scheduler_warning("public"))
		with self._tick(60):
			self.assertIsNone(scheduler_warning("private"))

	def test_site_config_value_does_not_silence_the_warning(self):
		"""R15a: ``bench schedule`` runs without a site and reads only common_site_config."""
		from dialogbrain.setup.connect import scheduler_tick, scheduler_warning

		with self._tick(None, site_value=60):
			self.assertEqual(scheduler_tick(), self._frappe_default())
			if self._frappe_default() > 60:
				self.assertIn("scheduler_tick_interval 60", scheduler_warning("private"))
		with self._tick(240, site_value=60):
			self.assertEqual(scheduler_tick(), 240)
			self.assertIn("scheduler_tick_interval 60", scheduler_warning("private"))
		with self._tick(60, site_value=240):
			self.assertEqual(scheduler_tick(), 60)
			self.assertIsNone(scheduler_warning("private"))

	@staticmethod
	def _frappe_default():
		try:
			from frappe.utils.scheduler import DEFAULT_SCHEDULER_TICK
		except ImportError:
			return 60
		return int(DEFAULT_SCHEDULER_TICK)

	def test_connect_and_set_network_mode_return_the_warning(self):
		frappe.db.set_single_value(SETTINGS, "network_mode", "private")
		client = _fake_client()
		with self._tick(240):
			result, _ = self._call("connect", client)
			self.assertIn("bench set-config -g scheduler_tick_interval 60", result["warning"])
			result, _ = self._call("set_network_mode", client, "public")
			self.assertIsNone(result["warning"])
			result, _ = self._call("set_network_mode", client, "private")
			self.assertIn("scheduler_tick_interval 60", result["warning"])
		with self._tick(60):
			result, _ = self._call("set_network_mode", client, "public")
			self.assertIsNone(result["warning"])

	def test_settings_form_gets_the_warning_on_load(self):
		from dialogbrain.setup.connect import scheduler_warning

		with self._tick(240):
			doc = frappe.get_single(SETTINGS)
			doc.run_method("onload")
			self.assertEqual(doc.get_onload().get("scheduler_warning"), scheduler_warning("private"))
		with self._tick(60):
			doc = frappe.get_single(SETTINGS)
			doc.run_method("onload")
			self.assertFalse(doc.get_onload().get("scheduler_warning"))
