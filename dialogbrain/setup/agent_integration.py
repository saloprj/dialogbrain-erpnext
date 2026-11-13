"""Register ERPNext's agent tools (``dialogbrain.agent_api``) as a DialogBrain HTTP
integration at Connect, and detect which DialogBrain capabilities are present (Plan 3 B3).

Flow (``on_connect``, called by ``connect._connect`` after the webhook step):

1. ``capabilities``: from the tool catalog plus one ``workspace.current`` probe.
2. Private network mode: stop here. DialogBrain can't reach ERPNext.
3. ``register``:
   a. Ensure the agent user and rotate its API key (``generate_keys``) on every
      (re)Connect. The key goes to DialogBrain as a custom ``Authorization: token k:s``
      header (``integrations.add_endpoints`` ``auth`` on create, ``integrations.set_auth``
      on an existing integration). ERPNext never stores the secret anywhere but the
      User's own Password field.
   b. ``integrations.add_endpoints`` with the two generic tools.
   c. When DialogBrain has thread context: a fresh 64-hex secret, then
      ``integrations.set_thread_context``. Only when that succeeds are the four
      customer tools added. A refusal (non-admin key) leaves the generic tools only.
   d. ``integrations.get_endpoints``: method -> tool-name map (stored in
      ``agent_tool_names``). Our endpoints that should not be there (customer tools
      after a refusal) are removed with ``integrations.remove_endpoints``.
   e. ``agents.list``, then ``agents.set_integration`` for every real agent, with
      ``denied_tools`` always explicit.
4. Problems never fail Connect: they land in ``agent_tools_status`` (shown in Settings).

Verified against the DialogBrain worktree ``~/projects/dialogbrain-erpnext-ctx``
(branch ``feature/erpnext-integration-context``, HEAD efdc167608), read-only:

* ``integrations.add_endpoints``: params ``base_url``, ``endpoints`` (``[{method, path,
  query?, request_body?, response_body?}]``), ``integration_id?``, ``auth?``
  (``{type: custom_headers, headers: {...}}``), ``name?``, ``identity?``,
  ``const_fields?``, ``extract?``, ``keepalive_operation?``. Merges additively into the
  integration (never drops tools); a NEW integration needs ``auth``. Response:
  ``{integration_id, created, tool_count, added_or_updated_tools: [names], note}``. The
  plan called the list ``new_tools``; the real key is ``added_or_updated_tools``.
  Tool names are operationIds DialogBrain derives from method + path; agents see them
  as ``ext<id>_<name>``. ``denied_tools`` matches the bare name.
  The request body schema is inferred from the ``request_body`` sample (arguments go
  inside a ``body`` argument and are sent as the JSON body). An endpoint ``description``
  is NOT read by this version: tools get no description. We send it anyway for a later
  DialogBrain that reads it.
* ``integrations.get_endpoints(integration_id | base_url)`` ->
  ``{integration_id, base_url, endpoint_count, endpoints: [{operationId, method, path}]}``.
* ``integrations.remove_endpoints(integration_id | base_url, operation_ids?, paths?)``
  -> ``{integration_id, removed, removed_count, remaining_tool_count}``.
* ``integrations.set_auth(integration_id | base_url, auth)`` -> ``{integration_id,
  auth_type, ...}``; ``custom_headers`` values are stored encrypted.
* ``integrations.set_thread_context(integration_id: int, enabled: bool, secret: str
  32-128 printable)`` -> ``{integration_id, enabled}``. Refuses an agent run and a key
  without the admin/owner role (``error_type permission_denied``). ``/execute`` returns
  only the error text, so a refusal is recognised by its wording ("admin"/"owner"/
  "person") or HTTP 403.
* ``agents.set_integration(agent_id: int, workspace_integration_id: int, enabled,
  denied_tools?)``: an upsert; omitting ``denied_tools`` auto-denies the tools its name
  regex thinks are writes, so we always pass it.
* ``agents.list(status?)`` -> ``{agents: [{id, name, status, is_template, ...}], total}``.
  System templates (``is_template``) are not workspace agents: ``set_integration``
  answers "Agent not found" for them, so they are skipped, as are archived agents.
* ``/api/tools/{id}/execute`` echoes ``request_id`` at the top level since Part A1; an
  older server ignores the field and echoes nothing. ``/api/tools/catalog`` (default
  ``planner_only=false``) lists planner-hidden tools, so the operator-only Part A tools
  show up there.
"""

import json
import secrets
import uuid
from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from dialogbrain.client import DialogBrainError

SETTINGS = "DialogBrain Settings"
ROLE = "System Manager"
PATH = "/api/method/dialogbrain.agent_api.{}"
STATUS_LENGTH = 1000
ADMIN_KEY_MESSAGE = "Customer data tools need an admin/owner DialogBrain API key"

# Tools ERPNext needs to register itself; without them registration is skipped.
REGISTRATION_TOOLS = ("integrations.add_endpoints", "agents.list", "agents.set_integration")
# Part A capabilities, detected from the catalog.
CAPABILITY_TOOLS = {
	"thread_context": "integrations.set_thread_context",
	"agent_pause": "threads.set_agent_paused",
	"full_drafts": "agents.get_draft",
}
CAPABILITY_LABELS = {
	"request_id": "request_id on sends",
	"thread_context": "signed thread context",
	"agent_pause": "take over / give back",
	"full_drafts": "full draft text",
}
_CUSTOMER_NOTE = (
	" Takes no argument that identifies the customer: the customer is identified from the chat"
	" (DialogBrain signs which conversation the call comes from). If the chat is not linked to a"
	" known customer it returns {\"identified\": false} and no data."
)
ENDPOINTS = {
	"get_item": {
		"scope": "generic",
		"write": False,
		"request_body": {"query": "laptop", "limit": 5},
		"description": (
			"Search the product catalogue by item code, name or description. Returns up to"
			" `limit` (default 5, max 20) items: item_code, item_name, price and currency from"
			" the default selling price list, stock_qty and in_stock. Use it to answer"
			" questions about products, prices and availability."
		),
	},
	"create_lead": {
		"scope": "generic",
		"write": True,
		"request_body": {
			"name": "Jane Doe",
			"phone": "+15550100",
			"email": "jane@example.com",
			"note": "Asked for a quote on 10 laptops",
		},
		"description": (
			"Record a sales lead in ERPNext for someone who is not a customer yet: `name` is"
			" required; `phone`, `email` and `note` are optional. Creates a Lead only (one per"
			" chat), nothing else. Returns the lead's name."
		),
	},
	"find_customer": {
		"scope": "customer",
		"write": False,
		"request_body": None,
		"description": (
			"Who the customer in this chat is: customer_name, party_type and the outstanding"
			" balance per currency." + _CUSTOMER_NOTE
		),
	},
	"order_status": {
		"scope": "customer",
		"write": False,
		"request_body": {"order_no": "SAL-ORD-2026-00001"},
		"description": (
			"Status of this customer's sales orders: the last 5, or the one named by the"
			" optional `order_no` if it belongs to them. Each has name, date, status,"
			" delivery_date, per_delivered, per_billed, grand_total and currency." + _CUSTOMER_NOTE
		),
	},
	"unpaid_invoices": {
		"scope": "customer",
		"write": False,
		"request_body": None,
		"description": (
			"This customer's unpaid sales invoices (up to 10): name, dates, grand total and"
			" outstanding amount." + _CUSTOMER_NOTE
		),
	},
	"create_draft_order": {
		"scope": "customer",
		"write": True,
		"request_body": {"items": [{"item_code": "LAPTOP-14", "qty": 1}]},
		"description": (
			"Prepare a DRAFT sales order for this customer from `items` (a list of"
			" {item_code, qty}). The draft is never submitted: a manager reviews and confirms"
			" it in ERPNext. Returns the draft's name and total." + _CUSTOMER_NOTE
		),
	},
}
GENERIC = tuple(m for m, e in ENDPOINTS.items() if e["scope"] == "generic")
CUSTOMER = tuple(m for m, e in ENDPOINTS.items() if e["scope"] == "customer")
WRITES = tuple(m for m, e in ENDPOINTS.items() if e["write"])


# --- helpers ------------------------------------------------------------------------


def _client(doc):
	from dialogbrain.setup.connect import _client as connect_client

	return connect_client(doc)


def _server_save(doc) -> None:
	from dialogbrain.setup.connect import _server_save as save

	save(doc)


def _scrub(text, *hidden) -> str:
	text = str(text)
	for value in hidden:
		if value:
			text = text.replace(value, "***")
	return text


def _endpoint(method: str) -> dict:
	spec = ENDPOINTS[method]
	ep = {"method": "POST", "path": PATH.format(method), "description": spec["description"]}
	if spec["request_body"] is not None:
		ep["request_body"] = spec["request_body"]
	return ep


def _method_of(path: str) -> "str | None":
	prefix = PATH.format("")
	if not (path or "").startswith(prefix):
		return None
	method = path[len(prefix) :]
	return method if method in ENDPOINTS else None


def _site(doc) -> str:
	return (doc.public_site_url or "").strip().rstrip("/")


def _names(doc) -> dict:
	try:
		names = json.loads(doc.agent_tool_names or "{}")
	except ValueError:
		return {}
	return names if isinstance(names, dict) else {}


def denied_tools(doc, names: "dict | None" = None) -> list:
	"""DialogBrain names of the write tools, unless the admin allowed writes."""
	if cint(doc.allow_agent_writes):
		return []
	names = _names(doc) if names is None else names
	return sorted(names[m] for m in WRITES if names.get(m))


def _is_admin_refusal(exc: DialogBrainError) -> bool:
	text = str(exc).lower()
	return exc.status == 403 or exc.code == "permission_denied" or any(
		word in text for word in ("admin", "owner", "permission", "person")
	)


# --- capabilities -------------------------------------------------------------------


def detect_request_id(client) -> bool:
	"""Part A1: ``/execute`` echoes a client-supplied ``request_id`` (one cheap
	``workspace.current`` call). An older server echoes nothing, or its own id."""
	rid = "erpnext-cap-" + uuid.uuid4().hex
	try:
		client.execute("workspace.current", {}, request_id=rid)
	except (DialogBrainError, TypeError):
		return False
	return getattr(client, "last_request_id", None) == rid


def capabilities(client, tools=None) -> dict:
	"""``{request_id, thread_context, agent_pause, full_drafts}`` booleans."""
	if tools is None:
		try:
			tools = set(client.list_tools() or ())
		except DialogBrainError:
			tools = set()
	caps = {key: tool in tools for key, tool in CAPABILITY_TOOLS.items()}
	caps["request_id"] = detect_request_id(client)
	return caps


# --- registration -------------------------------------------------------------------


def _rotate_agent_key(user: str) -> tuple:
	"""New API secret (and a key if the user has none) for the agent user."""
	from frappe.core.doctype.user.user import generate_keys

	keys = generate_keys(user) or {}
	api_key = keys.get("api_key") or frappe.db.get_value("User", user, "api_key")
	return api_key, keys["api_secret"]


def _attach_agents(client, iid: int, denied: list) -> tuple:
	"""``agents.set_integration`` on every real agent. Returns (attached, failed)."""
	agents = (client.execute("agents.list", {}) or {}).get("agents") or []
	attached = failed = 0
	for agent in agents:
		if agent.get("is_template") or agent.get("status") == "archived" or agent.get("id") is None:
			continue
		try:
			client.execute(
				"agents.set_integration",
				{
					"agent_id": int(agent["id"]),
					"workspace_integration_id": int(iid),
					"enabled": True,
					"denied_tools": list(denied),
				},
			)
			attached += 1
		except DialogBrainError:
			failed += 1
	return attached, failed


def register(doc, client, caps: dict, tools: set) -> dict:
	"""Register (or refresh) the integration; sets fields on ``doc`` (not saved).

	Raises DialogBrainError on a step that leaves nothing usable; ``on_connect`` turns
	that into a status line. Returns ``{tools, customer_tools, agents, problems}``."""
	from dialogbrain.agent_api import ensure_agent_user

	site = _site(doc)
	if not site:
		raise DialogBrainError("Public site URL is not set")
	user = ensure_agent_user()
	doc.erpnext_agent_user = user
	# ensure_agent_user wrote the Single directly, bumping ``modified``: keep ``doc`` saveable
	doc.modified = frappe.db.get_single_value(SETTINGS, "modified") or doc.modified
	api_key, api_secret = _rotate_agent_key(user)
	auth = {"type": "custom_headers", "headers": {"Authorization": f"token {api_key}:{api_secret}"}}
	hidden = (api_secret,)  # scrubbed from any message; the secret is never stored here
	problems = []

	iid = cint(doc.integration_id) or None
	if iid and "integrations.set_auth" in tools:
		try:
			client.execute("integrations.set_auth", {"integration_id": iid, "auth": auth})
		except DialogBrainError as exc:
			if "not_found" not in str(exc) and exc.status != 404:
				raise
			iid = None  # deleted in DialogBrain: create a new one
	params = {
		"base_url": site,
		"name": f"ERPNext ({urlsplit(site).hostname or site})",
		"endpoints": [_endpoint(m) for m in GENERIC],
	}
	if iid:
		params["integration_id"] = iid
		if "integrations.set_auth" not in tools:
			params["auth"] = auth
	else:
		params["auth"] = auth
	try:
		added = client.execute("integrations.add_endpoints", params) or {}
	except DialogBrainError as exc:
		if not iid or "not_found" not in str(exc):
			raise
		params.pop("integration_id")
		params["auth"] = auth
		added = client.execute("integrations.add_endpoints", params) or {}
	iid = int(added["integration_id"])
	added_names = list(added.get("added_or_updated_tools") or [])
	doc.integration_id = str(iid)

	customer_ok = False
	if caps.get("thread_context"):
		secret = secrets.token_hex(32)
		hidden += (secret,)
		try:
			client.execute(
				"integrations.set_thread_context", {"integration_id": iid, "enabled": True, "secret": secret}
			)
			doc.thread_context_secret = secret
			customer_ok = True
		except DialogBrainError as exc:
			problems.append(
				_(ADMIN_KEY_MESSAGE) if _is_admin_refusal(exc) else _scrub(f"thread context: {exc}", *hidden)
			)
	if customer_ok:
		added = client.execute(
			"integrations.add_endpoints",
			{"base_url": site, "integration_id": iid, "endpoints": [_endpoint(m) for m in CUSTOMER]},
		) or {}
		added_names += list(added.get("added_or_updated_tools") or [])

	wanted = set(GENERIC) | (set(CUSTOMER) if customer_ok else set())
	names = _tool_names(client, iid, tools, wanted, added_names, problems)
	doc.agent_tool_names = json.dumps(names, sort_keys=True)

	attached, failed = _attach_agents(client, iid, denied_tools(doc, names))
	if failed:
		problems.append(_("{0} agent(s) could not be given the tools").format(failed))
	return {"tools": len(names), "customer_tools": customer_ok, "agents": attached, "problems": problems}


def _tool_names(client, iid, tools, wanted, added_names, problems) -> dict:
	"""method -> DialogBrain tool name for the wanted methods; removes our unwanted ones.

	Primary source: ``integrations.get_endpoints`` (operationId by path). Fallback: the
	names ``add_endpoints`` returned, matched by method name."""
	names = {}
	if "integrations.get_endpoints" in tools:
		try:
			listed = client.execute("integrations.get_endpoints", {"integration_id": iid}) or {}
		except DialogBrainError as exc:
			listed = None
			problems.append(_("could not list the integration's endpoints: {0}").format(exc))
		if listed is not None:
			unwanted = []
			for ep in listed.get("endpoints") or []:
				method = _method_of(ep.get("path"))
				if not method or not ep.get("operationId"):
					continue
				if method in wanted:
					names[method] = ep["operationId"]
				else:
					unwanted.append(ep["operationId"])
			if unwanted:
				if "integrations.remove_endpoints" in tools:
					client.execute(
						"integrations.remove_endpoints", {"integration_id": iid, "operation_ids": unwanted}
					)
				else:
					problems.append(_("old customer tools could not be removed (no integrations.remove_endpoints)"))
			return names
	for method in wanted:
		key = method.replace("_", "").lower()
		for name in added_names:
			if str(name).replace("_", "").replace("-", "").lower().endswith(key):
				names[method] = name
				break
	return names


def _status(result: dict, caps: dict) -> str:
	parts = [
		_("Registered {0} tool(s) for {1} agent(s) on {2}").format(
			result["tools"], result["agents"], now_datetime().strftime("%Y-%m-%d %H:%M")
		)
	]
	parts += result["problems"]
	missing = [CAPABILITY_LABELS[k] for k in CAPABILITY_LABELS if not caps.get(k)]
	if missing:
		parts.append(_("missing in this DialogBrain: {0}").format(", ".join(missing)))
	return "; ".join(parts)


def on_connect(doc, client, tools: set) -> str:
	"""Capabilities + registration for ``connect._connect``. Never raises: the outcome
	is recorded on ``doc`` (``agent_capabilities``, ``agent_tools_status``; not saved)
	and returned as a one-line status."""
	tools = set(tools or ())
	caps = dict.fromkeys(("request_id", *CAPABILITY_TOOLS), False)
	caps["customer_tools"] = False
	hidden = ()
	try:
		caps.update(capabilities(client, tools))
		if (doc.network_mode or "public") == "private":
			status = _("Agent tools not registered: private network mode (DialogBrain can't reach ERPNext)")
		elif missing := [t for t in REGISTRATION_TOOLS if t not in tools]:
			status = _("Agent tools not registered: this DialogBrain API key has no access to {0}").format(
				", ".join(missing)
			)
		else:
			result = register(doc, client, caps, tools)
			caps["customer_tools"] = result["customer_tools"]
			status = _status(result, caps)
	except Exception as exc:  # noqa: BLE001 -- registration never fails Connect: sync is the core
		hidden = _secrets_of(doc)
		status = _("Agent tools registration failed: {0}").format(_scrub(exc, *hidden))
		frappe.log_error(title="DialogBrain agent tools registration", message=_scrub(frappe.get_traceback(), *hidden))
	caps["checked_at"] = str(now_datetime())
	doc.agent_capabilities = json.dumps(caps, sort_keys=True)
	doc.agent_tools_status = _scrub(status, *(hidden or _secrets_of(doc)))[:STATUS_LENGTH]
	return doc.agent_tools_status


def _secrets_of(doc) -> tuple:
	"""Current agent-user API secret and the context secret, to scrub from messages."""
	values = []
	user = doc.erpnext_agent_user
	if user:
		from frappe.utils.password import get_decrypted_password

		values.append(get_decrypted_password("User", user, "api_secret", raise_exception=False))
	if doc.thread_context_secret and not set(doc.thread_context_secret) <= {"*"}:
		values.append(doc.thread_context_secret)
	return tuple(v for v in values if v)


# --- allow_agent_writes / disconnect ---------------------------------------------------


def apply_denied_tools(doc, client=None) -> int:
	"""Re-send ``denied_tools`` to every agent after ``allow_agent_writes`` changed.
	Returns how many agents were updated; raises DialogBrainError."""
	if not (doc.connected and doc.integration_id) or (doc.network_mode or "public") == "private":
		return 0
	client = client or _client(doc)
	attached, failed = _attach_agents(client, int(doc.integration_id), denied_tools(doc))
	if failed:
		raise DialogBrainError(f"{failed} agent(s) could not be updated")
	return attached


@frappe.whitelist(methods=["POST"])
def set_agent_writes(allow) -> dict:
	"""The Settings toggle: store ``allow_agent_writes`` and re-apply ``denied_tools``."""
	frappe.only_for(ROLE)
	allow = 1 if cint(allow) else 0
	frappe.db.set_single_value(SETTINGS, "allow_agent_writes", allow)
	doc = frappe.get_single(SETTINGS)
	try:
		agents = apply_denied_tools(doc, _client(doc) if doc.connected and doc.integration_id else None)
	except DialogBrainError as exc:
		frappe.throw(
			_("Saved, but DialogBrain was not updated ({0}). Press Reconnect to retry.").format(str(exc)[:300])
		)
	return {"allow_agent_writes": allow, "agents": agents}


def on_disconnect(doc, client) -> "str | None":
	"""Revoke the agent user's key (local) and switch the integration off for every agent
	(best effort). Endpoints are left in DialogBrain; a later Connect reuses them.
	Returns a warning, or None."""
	user = doc.erpnext_agent_user
	if user and frappe.db.exists("User", user):
		u = frappe.get_doc("User", user)
		u.api_secret = frappe.generate_hash(length=32)  # never shared: revokes the old one
		u.save(ignore_permissions=True)
	if not doc.integration_id or client is None:
		return None
	try:
		agents = (client.execute("agents.list", {}) or {}).get("agents") or []
		for agent in agents:
			if agent.get("is_template") or agent.get("id") is None:
				continue
			try:
				client.execute(
					"agents.set_integration",
					{
						"agent_id": int(agent["id"]),
						"workspace_integration_id": int(doc.integration_id),
						"enabled": False,
						"denied_tools": denied_tools(doc),
					},
				)
			except DialogBrainError:
				pass  # e.g. never attached
	except DialogBrainError as exc:
		return _("Could not switch the ERPNext tools off for DialogBrain agents ({0}); the key is revoked.").format(
			str(exc)[:200]
		)
	return None
