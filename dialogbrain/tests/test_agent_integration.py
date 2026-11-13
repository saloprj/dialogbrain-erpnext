"""Task B3: ERPNext's agent tools registered as a DialogBrain HTTP integration at Connect.

DialogBrain is a stateful fake (no network). Its tool names are deliberately unlike
``dialogbrain.agent_api.<m>`` so the tests prove the app takes them from DialogBrain.
"""

import copy
import json
import logging
import re
from unittest.mock import patch

import frappe
from frappe.utils.password import get_decrypted_password

from dialogbrain.client import DialogBrainError
from dialogbrain.tests.utils import DialogBrainTestCase, load_fixture

SETTINGS = "DialogBrain Settings"
SITE = "https://erp.example.com"
PREFIX = "/api/method/dialogbrain.agent_api."
REQUIRED = {
	"workspace.current",
	"channels.list",
	"search.threads",
	"messages.read_history",
	"messages.send",
	"files.create_upload_url",
	"files.complete_upload",
}
REGISTRATION = {
	"integrations.add_endpoints",
	"integrations.get_endpoints",
	"integrations.remove_endpoints",
	"integrations.set_auth",
	"agents.list",
	"agents.set_integration",
}
PART_A = {"integrations.set_thread_context", "threads.set_agent_paused", "agents.get_draft"}
GENERIC = {"get_item", "create_lead"}
CUSTOMER = {"find_customer", "order_status", "unpaid_invoices", "create_draft_order"}
AGENTS = [
	{"id": 5, "name": "Sales", "status": "active", "is_template": False},
	{"id": 6, "name": "Support", "status": "paused", "is_template": False},
	{"id": 900, "name": "Doc agent", "status": "active", "is_template": True},
	{"id": 7, "name": "Old", "status": "archived", "is_template": False},
]


class FakeDialogBrain:
	"""Just enough of DialogBrain's tool API (shapes as in the Part A worktree)."""

	def __init__(self, tools=None, echo_request_id=True, thread_context_error=None):
		self.tools = set(REQUIRED | REGISTRATION | PART_A if tools is None else tools)
		self.echo = echo_request_id
		self.thread_context_error = thread_context_error
		self.calls = []
		self.integrations = {}  # id -> {base_url, name, auth, ops: {path: opid}, thread_context}
		self.attached = {}  # (agent_id, iid) -> {enabled, denied_tools}
		self.next_id = 77
		self.last_request_id = None
		self.fail = {}  # tool_id -> exception

	def list_tools(self):
		return set(self.tools)

	def list_webhooks(self):
		return []

	def create_webhook(self, url, events):
		return {"id": 41, "url": url, "secret": "whsec_" + "ab" * 32}

	def delete_webhook(self, webhook_id):
		pass

	def _opid(self, path):
		# not the method name: DialogBrain derives it (here: an opaque counter-ish id)
		return "op" + str(abs(hash(path)) % 100000) + "_" + path.rsplit(".", 1)[-1].replace("_", "")

	def execute(self, tool_id, params, thread_id=None, request_id=None):
		self.calls.append((tool_id, copy.deepcopy(params)))
		self.last_request_id = request_id if (self.echo and request_id) else ("srv-generated" if self.echo else None)
		if tool_id in self.fail:
			raise self.fail[tool_id]
		if tool_id not in self.tools:
			raise DialogBrainError(f"HTTP 404: tool {tool_id} not found", status=404)
		if tool_id == "workspace.current":
			return copy.deepcopy(load_fixture("workspace_current")["data"])
		if tool_id == "channels.list":
			return copy.deepcopy(load_fixture("channels_list")["data"])
		if tool_id == "integrations.add_endpoints":
			iid = params.get("integration_id")
			if iid is None:
				for k, v in self.integrations.items():
					if v["base_url"] == params["base_url"]:
						iid = k
			created = iid is None
			if created:
				if not params.get("auth"):
					raise DialogBrainError("auth_required: new integration needs auth")
				iid = self.next_id
				self.next_id += 1
				self.integrations[iid] = {
					"base_url": params["base_url"],
					"name": params.get("name"),
					"auth": None,
					"ops": {},
					"thread_context": None,
				}
			elif iid not in self.integrations:
				raise DialogBrainError(f"not_found: integration {iid}")
			integ = self.integrations[iid]
			if params.get("auth"):
				integ["auth"] = copy.deepcopy(params["auth"])
			names = []
			for ep in params["endpoints"]:
				assert ep["method"] == "POST", ep
				assert "query" not in ep, "agent API refuses query strings"
				integ["ops"][ep["path"]] = self._opid(ep["path"])
				names.append(integ["ops"][ep["path"]])
			return {
				"integration_id": iid,
				"created": created,
				"tool_count": len(integ["ops"]),
				"added_or_updated_tools": names,
				"note": f"prefix ext{iid}_",
			}
		if tool_id == "integrations.get_endpoints":
			integ = self.integrations[params["integration_id"]]
			ops = [{"operationId": o, "method": "POST", "path": p} for p, o in integ["ops"].items()]
			return {"integration_id": params["integration_id"], "endpoint_count": len(ops), "endpoints": ops}
		if tool_id == "integrations.remove_endpoints":
			integ = self.integrations[params["integration_id"]]
			drop = set(params.get("operation_ids") or [])
			removed = [o for o in integ["ops"].values() if o in drop]
			integ["ops"] = {p: o for p, o in integ["ops"].items() if o not in drop}
			return {"removed": removed, "removed_count": len(removed)}
		if tool_id == "integrations.set_auth":
			iid = params["integration_id"]
			if iid not in self.integrations:
				raise DialogBrainError(f"not_found: integration {iid}")
			self.integrations[iid]["auth"] = copy.deepcopy(params["auth"])
			return {"integration_id": iid, "auth_type": params["auth"]["type"]}
		if tool_id == "integrations.set_thread_context":
			if self.thread_context_error:
				raise self.thread_context_error
			self.integrations[params["integration_id"]]["thread_context"] = (params["enabled"], params.get("secret"))
			return {"integration_id": params["integration_id"], "enabled": params["enabled"]}
		if tool_id == "agents.list":
			return {"agents": copy.deepcopy(AGENTS), "total": len(AGENTS)}
		if tool_id == "agents.set_integration":
			if any(a["id"] == params["agent_id"] and a["is_template"] for a in AGENTS):
				raise DialogBrainError(f"Agent {params['agent_id']} not found")
			self.attached[(params["agent_id"], params["workspace_integration_id"])] = {
				"enabled": params["enabled"],
				"denied_tools": list(params["denied_tools"]),
			}
			return dict(params)
		raise AssertionError(f"unexpected tool {tool_id}")

	# helpers for assertions
	def called(self, tool_id):
		return [p for t, p in self.calls if t == tool_id]

	def methods(self, iid):
		return {p[len(PREFIX) :] for p in self.integrations[iid]["ops"]}

	def name_of(self, iid, method):
		return self.integrations[iid]["ops"][PREFIX + method]


class _AgentIntegrationCase(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		doc = frappe.get_single(SETTINGS)
		doc.update(
			{
				"base_url": "https://api.example.com",
				"api_key": "db_live_testkey000000000000",
				"network_mode": "public",
				"public_site_url": SITE + "/",
				"webhook_id": None,
				"webhook_secret": None,
				"connected": 0,
				"integration_id": None,
				"agent_capabilities": None,
				"agent_tools_status": None,
				"agent_tool_names": None,
				"thread_context_secret": None,
				"allow_agent_writes": 0,
			}
		)
		doc.flags.dialogbrain_server_write = True
		doc.save()
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def tearDown(self):
		frappe.set_user("Administrator")

	def _connect(self, fake):
		from dialogbrain.setup import connect as mod

		with patch.object(mod, "DialogBrainClient", return_value=fake), patch("frappe.enqueue"):
			return mod.connect()

	def _get(self, field):
		return frappe.db.get_single_value(SETTINGS, field)

	def _caps(self):
		return json.loads(self._get("agent_capabilities") or "{}")

	def _agent_secret(self):
		user = self._get("erpnext_agent_user")
		return frappe.db.get_value("User", user, "api_key"), get_decrypted_password(
			"User", user, "api_secret", raise_exception=False
		)


class TestAgentIntegration(_AgentIntegrationCase):
	# --- full DialogBrain -------------------------------------------------------------

	def test_full_capabilities_register_six_tools_context_and_every_agent(self):
		fake = FakeDialogBrain()
		result = self._connect(fake)

		self.assertEqual(self._get("connected"), 1)
		iid = int(self._get("integration_id"))
		self.assertEqual(fake.methods(iid), GENERIC | CUSTOMER)
		integ = fake.integrations[iid]
		self.assertEqual(integ["base_url"], SITE)
		self.assertEqual(integ["name"], "ERPNext (erp.example.com)")

		# the key DialogBrain calls with is the agent user's, freshly generated
		key, secret = self._agent_secret()
		self.assertTrue(key and secret)
		self.assertEqual(
			integ["auth"], {"type": "custom_headers", "headers": {"Authorization": f"token {key}:{secret}"}}
		)

		# thread context: a 64-hex secret, stored in ERPNext and sent once to DialogBrain
		stored = frappe.get_single(SETTINGS).get_password("thread_context_secret", raise_exception=False)
		self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", stored or ""))
		self.assertEqual(integ["thread_context"], (True, stored))

		# tool names come from DialogBrain
		names = json.loads(self._get("agent_tool_names"))
		self.assertEqual(set(names), GENERIC | CUSTOMER)
		for method, name in names.items():
			self.assertEqual(name, fake.name_of(iid, method))
			self.assertNotIn("dialogbrain.agent_api", name)

		# every real agent (not templates, not archived) gets it, writes denied
		denied = sorted([names["create_lead"], names["create_draft_order"]])
		self.assertEqual(set(fake.attached), {(5, iid), (6, iid)})
		for row in fake.attached.values():
			self.assertTrue(row["enabled"])
			self.assertEqual(sorted(row["denied_tools"]), denied)
		for params in fake.called("agents.set_integration"):
			self.assertIn("denied_tools", params)
			self.assertEqual(params["workspace_integration_id"], iid)

		caps = self._caps()
		self.assertTrue(caps["request_id"])
		self.assertTrue(caps["thread_context"])
		self.assertTrue(caps["agent_pause"])
		self.assertTrue(caps["full_drafts"])
		self.assertTrue(caps["customer_tools"])
		self.assertIn("6", self._get("agent_tools_status"))
		self.assertTrue(result.get("agent_tools"))

	def test_endpoints_have_json_bodies_and_llm_descriptions(self):
		fake = FakeDialogBrain()
		self._connect(fake)
		endpoints = [ep for params in fake.called("integrations.add_endpoints") for ep in params["endpoints"]]
		by_method = {ep["path"][len(PREFIX) :]: ep for ep in endpoints}
		self.assertEqual(set(by_method), GENERIC | CUSTOMER)
		self.assertEqual(set(by_method["get_item"]["request_body"]), {"query", "limit"})
		self.assertEqual(set(by_method["create_lead"]["request_body"]), {"name", "phone", "email", "note"})
		self.assertEqual(set(by_method["order_status"]["request_body"]), {"order_no"})
		self.assertEqual(set(by_method["create_draft_order"]["request_body"]), {"items"})
		for method in ("find_customer", "unpaid_invoices"):
			self.assertFalse(by_method[method].get("request_body"))
		for method in CUSTOMER:
			self.assertIn("identified from the chat", by_method[method]["description"])
		for method in ("create_lead", "create_draft_order"):
			self.assertRegex(by_method[method]["description"].lower(), r"draft|lead")

	# --- DialogBrain without Part A (Review Focus 5) ----------------------------------

	def test_without_part_a_only_generic_tools_and_connect_succeeds(self):
		fake = FakeDialogBrain(tools=REQUIRED | REGISTRATION, echo_request_id=False)
		self._connect(fake)

		self.assertEqual(self._get("connected"), 1)
		iid = int(self._get("integration_id"))
		self.assertEqual(fake.methods(iid), GENERIC)
		self.assertFalse(fake.called("integrations.set_thread_context"))
		names = json.loads(self._get("agent_tool_names"))
		self.assertEqual(set(names), GENERIC)
		for row in fake.attached.values():
			self.assertEqual(row["denied_tools"], [names["create_lead"]])
		caps = self._caps()
		self.assertEqual(
			{k: caps[k] for k in ("request_id", "thread_context", "agent_pause", "full_drafts", "customer_tools")},
			dict.fromkeys(("request_id", "thread_context", "agent_pause", "full_drafts", "customer_tools"), False),
		)
		self.assertIn("missing", self._get("agent_tools_status").lower())

	def test_old_server_echoing_its_own_request_id_is_not_the_capability(self):
		fake = FakeDialogBrain()
		fake.echo = False

		def execute(tool_id, params, thread_id=None, request_id=None):
			result = FakeDialogBrain.execute(fake, tool_id, params, thread_id, request_id)
			fake.last_request_id = "something-else"
			return result

		fake.execute = execute
		self._connect(fake)
		self.assertFalse(self._caps()["request_id"])

	def test_thread_context_refused_for_non_admin_key_registers_generic_only(self):
		err = DialogBrainError("Setting an integration's thread context needs the admin or owner role.")
		fake = FakeDialogBrain(thread_context_error=err)
		self._connect(fake)

		self.assertEqual(self._get("connected"), 1)
		iid = int(self._get("integration_id"))
		self.assertEqual(fake.methods(iid), GENERIC)
		self.assertIn("Customer data tools need an admin/owner DialogBrain API key", self._get("agent_tools_status"))
		self.assertFalse(self._caps()["customer_tools"])

	def test_registration_failure_does_not_fail_connect(self):
		fake = FakeDialogBrain()
		fake.fail["integrations.add_endpoints"] = DialogBrainError("ssrf_blocked: private host")
		result = self._connect(fake)
		self.assertEqual(self._get("connected"), 1)
		self.assertTrue(self._get("webhook_id"))
		self.assertIn("ssrf_blocked", self._get("agent_tools_status"))
		self.assertTrue(result["agent_tools"])

	def test_key_without_registration_tools_is_reported_not_fatal(self):
		fake = FakeDialogBrain(tools=REQUIRED)
		self._connect(fake)
		self.assertEqual(self._get("connected"), 1)
		self.assertFalse(fake.called("integrations.add_endpoints"))
		self.assertIn("integrations.add_endpoints", self._get("agent_tools_status"))

	# --- private mode -----------------------------------------------------------------

	def test_private_mode_skips_registration(self):
		frappe.db.set_single_value(SETTINGS, "network_mode", "private")
		fake = FakeDialogBrain()
		self._connect(fake)
		self.assertEqual(self._get("connected"), 1)
		for tool in ("integrations.add_endpoints", "integrations.set_thread_context", "agents.set_integration"):
			self.assertFalse(fake.called(tool), tool)
		self.assertIn("private", self._get("agent_tools_status").lower())

	# --- idempotency ------------------------------------------------------------------

	def test_reconnect_reuses_integration_rotates_key_and_replaces_endpoints(self):
		fake = FakeDialogBrain()
		self._connect(fake)
		iid = int(self._get("integration_id"))
		_first_key, first_secret = self._agent_secret()
		first_ctx = fake.integrations[iid]["thread_context"][1]

		# second Connect: the key lost the admin role, so customer tools must go away
		fake.thread_context_error = DialogBrainError("needs the admin or owner role")
		self._connect(fake)

		self.assertEqual(int(self._get("integration_id")), iid)
		self.assertEqual(list(fake.integrations), [iid])
		self.assertEqual(fake.methods(iid), GENERIC)
		key, secret = self._agent_secret()
		self.assertNotEqual(secret, first_secret)
		self.assertEqual(fake.integrations[iid]["auth"]["headers"]["Authorization"], f"token {key}:{secret}")
		self.assertTrue(fake.called("integrations.set_auth"))
		self.assertEqual(set(fake.attached), {(5, iid), (6, iid)})
		self.assertTrue(first_ctx)

		# third Connect with the admin key back: six tools again, same integration
		fake.thread_context_error = None
		self._connect(fake)
		self.assertEqual(list(fake.integrations), [iid])
		self.assertEqual(fake.methods(iid), GENERIC | CUSTOMER)

	def test_reconnect_after_integration_deleted_in_dialogbrain_creates_a_new_one(self):
		fake = FakeDialogBrain()
		self._connect(fake)
		old = int(self._get("integration_id"))
		del fake.integrations[old]
		self._connect(fake)
		new = int(self._get("integration_id"))
		self.assertNotEqual(new, old)
		self.assertEqual(fake.methods(new), GENERIC | CUSTOMER)

	# --- allow_agent_writes -----------------------------------------------------------

	def test_toggling_writes_reapplies_denied_tools(self):
		from dialogbrain.setup import agent_integration as mod

		fake = FakeDialogBrain()
		self._connect(fake)
		iid = int(self._get("integration_id"))
		names = json.loads(self._get("agent_tool_names"))

		with patch.object(mod, "_client", return_value=fake):
			mod.set_agent_writes(1)
		self.assertEqual(self._get("allow_agent_writes"), 1)
		for agent in (5, 6):
			self.assertEqual(fake.attached[(agent, iid)]["denied_tools"], [])

		with patch.object(mod, "_client", return_value=fake):
			mod.set_agent_writes(0)
		for agent in (5, 6):
			self.assertEqual(
				sorted(fake.attached[(agent, iid)]["denied_tools"]),
				sorted([names["create_lead"], names["create_draft_order"]]),
			)

	def test_set_agent_writes_is_system_manager_only(self):
		from dialogbrain.setup import agent_integration as mod

		email = "db-agent-int-desk@example.com"
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{"doctype": "User", "email": email, "first_name": "Desk", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
		frappe.set_user(email)
		with self.assertRaises(frappe.PermissionError):
			mod.set_agent_writes(1)

	def test_writes_allowed_before_connect_attach_with_empty_denied_tools(self):
		frappe.db.set_single_value(SETTINGS, "allow_agent_writes", 1)
		fake = FakeDialogBrain()
		self._connect(fake)
		for row in fake.attached.values():
			self.assertEqual(row["denied_tools"], [])

	# --- secrets ----------------------------------------------------------------------

	def test_secrets_never_logged_returned_or_stored_in_status(self):
		fake = FakeDialogBrain()
		logged = []

		class Grab(logging.Handler):
			def emit(self, record):
				logged.append(record.getMessage())

		handler = Grab()
		root = logging.getLogger()
		root.addHandler(handler)
		try:
			with patch("frappe.log_error") as log_error:
				result = self._connect(fake)
		finally:
			root.removeHandler(handler)

		_key, secret = self._agent_secret()
		ctx = frappe.get_single(SETTINGS).get_password("thread_context_secret", raise_exception=False)
		public = json.dumps(result, default=str) + " ".join(logged) + str(log_error.call_args_list)
		public += str(self._get("agent_tools_status")) + str(self._get("agent_capabilities"))
		public += str(self._get("agent_tool_names"))
		for value in (secret, ctx):
			self.assertTrue(value)
			self.assertNotIn(value, public)
		# the Password field keeps the context secret out of the plain column
		self.assertNotEqual(self._get("thread_context_secret"), ctx)

	def test_error_echoing_a_secret_is_scrubbed_from_status(self):
		fake = FakeDialogBrain()
		original = fake.execute

		def execute(tool_id, params, thread_id=None, request_id=None):
			if tool_id == "agents.list":
				integ = next(iter(fake.integrations.values()))
				raise DialogBrainError("boom " + integ["auth"]["headers"]["Authorization"])
			return original(tool_id, params, thread_id, request_id)

		fake.execute = execute
		self._connect(fake)
		_key, secret = self._agent_secret()
		self.assertIn("boom", self._get("agent_tools_status"))
		self.assertNotIn(secret, self._get("agent_tools_status"))

	# --- disconnect -------------------------------------------------------------------

	def test_disconnect_detaches_agents_and_revokes_the_agent_key(self):
		from dialogbrain.setup import connect as mod

		fake = FakeDialogBrain()
		self._connect(fake)
		iid = int(self._get("integration_id"))
		_key, secret = self._agent_secret()

		with patch.object(mod, "DialogBrainClient", return_value=fake):
			mod.disconnect()

		self.assertEqual(self._get("connected"), 0)
		for agent in (5, 6):
			self.assertFalse(fake.attached[(agent, iid)]["enabled"])
		_key2, secret2 = self._agent_secret()
		self.assertNotEqual(secret2, secret)

	def test_disconnect_survives_dialogbrain_errors(self):
		from dialogbrain.setup import connect as mod

		fake = FakeDialogBrain()
		self._connect(fake)
		fake.fail["agents.list"] = DialogBrainError("down")
		with patch.object(mod, "DialogBrainClient", return_value=fake):
			mod.disconnect()
		self.assertEqual(self._get("connected"), 0)


class TestAgentIntegrationFixRound1(_AgentIntegrationCase):
	"""Review fix round 1: key rotation rollback, fail-closed denied_tools, R10 schemas."""

	# --- 1. a failed auth push keeps the previous key ---------------------------------

	def test_set_auth_failure_on_reconnect_keeps_the_previous_key(self):
		fake = FakeDialogBrain()
		self._connect(fake)
		iid = int(self._get("integration_id"))
		key, secret = self._agent_secret()

		fake.fail["integrations.set_auth"] = DialogBrainError("DialogBrain HTTP 503: unavailable", status=503)
		result = self._connect(fake)

		self.assertEqual(self._get("connected"), 1)
		self.assertEqual(self._agent_secret(), (key, secret))
		self.assertEqual(fake.integrations[iid]["auth"]["headers"]["Authorization"], f"token {key}:{secret}")
		self.assertIn("previous key", self._get("agent_tools_status"))
		self.assertEqual(self._caps()["status"], "problems")
		self.assertFalse(result["agent_tools_ok"])
		# the rest still ran on the old key
		self.assertEqual(fake.methods(iid), GENERIC | CUSTOMER)
		self.assertEqual(set(fake.attached), {(5, iid), (6, iid)})

	def test_add_endpoints_auth_failure_keeps_the_previous_key(self):
		fake = FakeDialogBrain(tools=REQUIRED | (REGISTRATION - {"integrations.set_auth"}) | PART_A)
		self._connect(fake)
		key, secret = self._agent_secret()
		fake.fail["integrations.add_endpoints"] = DialogBrainError("DialogBrain HTTP 502", status=502)
		self._connect(fake)
		self.assertEqual(self._get("connected"), 1)
		self.assertEqual(self._agent_secret(), (key, secret))
		self.assertEqual(self._caps()["status"], "problems")

	def test_ok_status_flag(self):
		result = self._connect(FakeDialogBrain())
		self.assertEqual(self._caps()["status"], "ok")
		self.assertTrue(result["agent_tools_ok"])

	def test_private_mode_status_flag_is_skipped(self):
		frappe.db.set_single_value(SETTINGS, "network_mode", "private")
		self._connect(FakeDialogBrain())
		self.assertEqual(self._caps()["status"], "skipped")

	# --- 2. denied_tools fails closed -------------------------------------------------

	def test_unidentified_write_tools_attach_agents_disabled(self):
		fake = FakeDialogBrain()
		fake.fail["integrations.get_endpoints"] = DialogBrainError("DialogBrain HTTP 500", status=500)
		fake._opid = lambda path: "zz" + str(sum(map(ord, path)))  # fallback cannot match these
		self._connect(fake)

		iid = int(self._get("integration_id"))
		self.assertEqual(set(fake.attached), {(5, iid), (6, iid)})
		for row in fake.attached.values():
			self.assertFalse(row["enabled"])
		self.assertIn("write tools could not be identified; agents not attached", self._get("agent_tools_status"))
		self.assertEqual(self._caps()["status"], "problems")

	def test_unidentified_write_tools_when_writes_allowed_still_attach(self):
		frappe.db.set_single_value(SETTINGS, "allow_agent_writes", 1)
		fake = FakeDialogBrain()
		fake.fail["integrations.get_endpoints"] = DialogBrainError("DialogBrain HTTP 500", status=500)
		fake._opid = lambda path: "zz" + str(sum(map(ord, path)))
		self._connect(fake)
		for row in fake.attached.values():
			self.assertTrue(row["enabled"])
			self.assertEqual(row["denied_tools"], [])

	def test_toggle_refused_with_empty_tool_map(self):
		from dialogbrain.setup import agent_integration as mod

		fake = FakeDialogBrain()
		self._connect(fake)
		frappe.db.set_single_value(SETTINGS, "agent_tool_names", "")
		before = len(fake.called("agents.set_integration"))
		for allow in (1, 0):
			frappe.db.set_single_value(SETTINGS, "allow_agent_writes", 1 - allow)
			with patch.object(mod, "_client", return_value=fake), self.assertRaises(frappe.ValidationError) as cm:
				mod.set_agent_writes(allow)
			self.assertIn("Not changed", str(cm.exception))
			self.assertEqual(self._get("allow_agent_writes"), 1 - allow)
		self.assertEqual(len(fake.called("agents.set_integration")), before)

	def test_toggle_dialogbrain_failure_is_not_changed(self):
		from dialogbrain.setup import agent_integration as mod

		fake = FakeDialogBrain()
		self._connect(fake)
		fake.fail["agents.set_integration"] = DialogBrainError("down")
		with patch.object(mod, "_client", return_value=fake), self.assertRaises(frappe.ValidationError) as cm:
			mod.set_agent_writes(1)
		self.assertIn("Not changed: DialogBrain could not be updated", str(cm.exception))
		self.assertEqual(self._get("allow_agent_writes"), 0)

	def test_stale_customer_endpoints_unlisted_is_a_problem(self):
		fake = FakeDialogBrain()
		self._connect(fake)
		fake.thread_context_error = DialogBrainError("refused", error_type="permission_denied")
		fake.fail["integrations.get_endpoints"] = DialogBrainError("DialogBrain HTTP 500", status=500)
		self._connect(fake)
		self.assertIn("customer tools may still be registered", self._get("agent_tools_status"))

	def test_remove_endpoints_failure_is_recorded_not_fatal(self):
		fake = FakeDialogBrain()
		self._connect(fake)
		iid = int(self._get("integration_id"))
		fake.thread_context_error = DialogBrainError("refused", error_type="permission_denied")
		fake.fail["integrations.remove_endpoints"] = DialogBrainError("DialogBrain HTTP 500", status=500)
		self._connect(fake)
		self.assertIn("could not be removed", self._get("agent_tools_status"))
		self.assertEqual(self._caps()["status"], "problems")
		names = json.loads(self._get("agent_tool_names"))
		self.assertEqual(set(names), GENERIC)
		for row in fake.attached.values():
			self.assertTrue(row["enabled"])
		self.assertEqual(set(fake.attached), {(5, iid), (6, iid)})

	# --- 3. disconnect best effort ----------------------------------------------------

	def test_disconnect_survives_agent_user_save_failure(self):
		from dialogbrain.setup import connect as mod

		fake = FakeDialogBrain()
		self._connect(fake)
		with patch.object(mod, "DialogBrainClient", return_value=fake), patch(
			"frappe.core.doctype.user.user.User.save", side_effect=frappe.ValidationError("nope")
		):
			mod.disconnect()
		self.assertEqual(self._get("connected"), 0)

	# --- 4. R10: request_schema + description, error_type -----------------------------

	def test_endpoints_carry_request_schema_and_short_descriptions(self):
		fake = FakeDialogBrain()
		self._connect(fake)
		eps = {
			ep["path"][len(PREFIX) :]: ep
			for params in fake.called("integrations.add_endpoints")
			for ep in params["endpoints"]
		}
		for ep in eps.values():
			self.assertLessEqual(len(ep["description"]), 1024)
		for method in ("get_item", "create_lead", "order_status", "create_draft_order"):
			schema = eps[method]["request_schema"]
			self.assertEqual(schema["type"], "object")
			self.assertEqual(set(schema["properties"]), set(eps[method]["request_body"]))
		self.assertEqual(eps["get_item"]["request_schema"]["properties"]["limit"]["type"], "integer")
		self.assertEqual(eps["create_lead"]["request_schema"]["required"], ["name"])
		items = eps["create_draft_order"]["request_schema"]["properties"]["items"]
		self.assertEqual(items["type"], "array")
		self.assertEqual(items["items"]["properties"]["item_code"]["type"], "string")
		self.assertEqual(items["items"]["properties"]["qty"]["type"], "number")
		self.assertEqual(sorted(items["items"]["required"]), ["item_code", "qty"])
		self.assertIn("items", eps["create_draft_order"]["request_schema"]["required"])

	def test_permission_denied_error_type_is_the_admin_refusal(self):
		fake = FakeDialogBrain(thread_context_error=DialogBrainError("refused", error_type="permission_denied"))
		self._connect(fake)
		self.assertIn("Customer data tools need an admin/owner DialogBrain API key", self._get("agent_tools_status"))

	def test_other_error_type_is_not_the_admin_refusal_even_if_worded_so(self):
		err = DialogBrainError("permission wording but bad secret", error_type="invalid_arguments")
		fake = FakeDialogBrain(thread_context_error=err)
		self._connect(fake)
		status = self._get("agent_tools_status")
		self.assertNotIn("admin/owner", status)
		self.assertIn("thread context:", status)
