"""Connect / Disconnect / Re-sync / network mode / webhook health (DialogBrain Settings).

All entry points are System Manager only. Server-managed Settings fields are written
either through ``frappe.db.set_single_value`` (``set_setting``) or a ``doc.save()`` with
``doc.flags.dialogbrain_server_write`` set, because ``DialogBrainSettings.validate``
restores them from the database on any other save (R14).
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from dialogbrain.client import DialogBrainClient, DialogBrainError
from dialogbrain.setup import agent_integration
from dialogbrain.sync.backfill import enqueue_backfill, set_setting
from dialogbrain.sync.upsert import upsert_channel

SETTINGS = "DialogBrain Settings"
ROLE = "System Manager"
WEBHOOK_PATH = "/api/method/dialogbrain.api.webhook.receive"
MODES = ("public", "private")
REQUIRED_TOOLS = {
	"workspace.current",
	"channels.list",
	"search.threads",
	"messages.read_history",
	"messages.send",
	"files.create_upload_url",
	"files.complete_upload",
}
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
# Fields only server code may change (R14).
SERVER_FIELDS = (
	"workspace_id",
	"workspace_name",
	"webhook_id",
	"webhook_secret",
	"webhook_status",
	"last_reconcile_at",
	"reconcile_failures",
	"reconcile_status",
	"backfill_status",
	"connected",
	"oauth_client_id",
	"oauth_redirect_uri",
	"oauth_base_url",
	# agent tools (Plan 3); allow_agent_writes is the admin's switch, not server-managed
	"erpnext_agent_user",
	"integration_id",
	"thread_context_secret",
	"agent_capabilities",
	"agent_tools_status",
	"agent_tool_names",
)
DELIVERED = "delivered"
IN_FLIGHT = ("pending", "queued", "retrying")
STATUS_LENGTH = 500
PRIVATE_STATUS = "private mode: no webhook (reconcile polls every minute)"


# --- scheduler tick (R15) -----------------------------------------------------------


def scheduler_tick() -> int:
	"""Seconds between scheduler ticks as ``bench schedule`` sees them (R15a).

	The scheduler runs without a site and reads ``scheduler_tick_interval`` only from
	common_site_config.json, once at start; a per-site value has no effect. Absent there:
	the Frappe default (240 s on v16, 60 s on v15)."""
	from frappe.utils import cint

	configured = cint((frappe.get_common_site_config() or {}).get("scheduler_tick_interval"))
	if configured:
		return configured
	try:
		from frappe.utils.scheduler import DEFAULT_SCHEDULER_TICK
	except ImportError:  # Frappe v15 has no constant: the tick is 60 s
		return 60
	return int(DEFAULT_SCHEDULER_TICK)


def scheduler_warning(mode: str) -> "str | None":
	"""Private mode polls DialogBrain every minute, which needs a 60 s scheduler tick. The
	app does not edit config; it names the commands for the admin."""
	tick = scheduler_tick()
	if mode != "private" or tick <= 60:
		return None
	return _(
		"The scheduler runs every {0} s, so private mode picks up new messages only every {0} s"
		" instead of every minute. Run: {1}, then restart the scheduler ({2}, or restart the"
		" scheduler process/container)."
	).format(tick, "bench set-config -g scheduler_tick_interval 60", "bench restart")


# --- helpers --------------------------------------------------------------------


def _client(doc) -> DialogBrainClient:
	api_key = doc.get_password("api_key", raise_exception=False)
	if not doc.base_url or not api_key:
		frappe.throw(_("DialogBrain Settings: base URL and API key are required"))
	return DialogBrainClient(doc.base_url, api_key)


def _server_save(doc) -> None:
	doc.flags.dialogbrain_server_write = True
	doc.save(ignore_permissions=True)


def webhook_url(doc) -> str:
	site = (doc.public_site_url or "").strip().rstrip("/")
	if not site:
		frappe.throw(_("Public site URL is required in public mode (DialogBrain delivers webhooks there)"))
	return site + WEBHOOK_PATH


def _delete_webhook(client, webhook_id) -> None:
	"""Delete a subscription; one that is already gone (404) is fine."""
	try:
		client.delete_webhook(webhook_id)
	except DialogBrainError as exc:
		if exc.status != 404:
			raise


def configure_webhook(doc, mode: str, client=None, hooks=None) -> None:
	"""Make the subscription match ``mode`` and set the webhook fields on ``doc`` (not saved).

	``public``: drop our previous subscription and any other one for the same URL, then
	create a fresh one (its secret is only returned on create). ``private``: drop ours.
	"""
	client = client or _client(doc)
	if mode == "public":
		url = webhook_url(doc)  # validate before touching anything
	if doc.webhook_id:
		_delete_webhook(client, doc.webhook_id)
	doc.webhook_id = None
	doc.webhook_secret = None
	if mode != "public":
		doc.webhook_status = PRIVATE_STATUS
		return
	if hooks is None:
		hooks = client.list_webhooks() or []
	for hook in hooks:
		if (hook.get("url") or "").rstrip("/") == url:
			_delete_webhook(client, hook.get("id"))
	created = client.create_webhook(url, EVENTS)
	if not isinstance(created, dict) or not created.get("id") or not created.get("secret"):
		frappe.throw(_("DialogBrain did not return a webhook id and secret"))
	doc.webhook_id = str(created["id"])
	doc.webhook_secret = created["secret"]
	doc.webhook_status = f"active: created {now_datetime():%Y-%m-%d %H:%M:%S}"


# --- whitelisted ------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def connect() -> dict:
	frappe.only_for(ROLE)
	return _connect(frappe.get_single(SETTINGS))


def _connect(doc) -> dict:
	"""Verify the stored key, mirror channels, set up the webhook and start the backfill.

	Shared by ``connect`` (pasted key) and the OAuth callback; the caller checks the role."""
	mode = doc.network_mode or "public"
	if mode == "public":
		webhook_url(doc)
	client = _client(doc)

	try:
		workspace = client.execute("workspace.current", {}) or {}
	except DialogBrainError:
		frappe.throw(_("Invalid API key or DialogBrain unreachable"))

	missing = []
	tools = set()
	try:
		tools = set(client.list_tools() or ())
		missing += sorted(REQUIRED_TOOLS - tools)
	except DialogBrainError:
		missing.append(_("tool catalog (GET /api/tools/catalog)"))
	try:
		hooks = client.list_webhooks() or []
	except DialogBrainError:
		hooks = None
		missing.append(_("webhooks API (GET /api/v1/webhooks)"))
	if missing:
		frappe.throw(_("This DialogBrain API key has no access to: {0}").format(", ".join(missing)))

	channels = (client.execute("channels.list", {}) or {}).get("accounts") or []
	for account in channels:
		upsert_channel(account.get("account_id"), account.get("channel"), account.get("display_name"))

	configure_webhook(doc, mode, client, hooks)
	# Agent tools (Plan 3): never fails Connect; the outcome is in agent_tools_status.
	agent_tools = agent_integration.on_connect(doc, client, tools)
	doc.workspace_id = str(workspace.get("id") or "") or None
	doc.workspace_name = workspace.get("name")
	doc.connected = 1
	doc.last_reconcile_at = now_datetime()
	_server_save(doc)
	enqueue_backfill()
	return {
		"workspace": workspace.get("name"),
		"channels": len(channels),
		"network_mode": mode,
		"warning": scheduler_warning(mode),
		"agent_tools": agent_tools,
	}


@frappe.whitelist(methods=["POST"])
def disconnect() -> None:
	"""Stop syncing. The subscription is deleted; mirrored data stays."""
	frappe.only_for(ROLE)
	doc = frappe.get_single(SETTINGS)
	try:
		client = _client(doc)
	except frappe.ValidationError:
		client = None
	# Agent tools: the agent user's key is revoked (local) and the integration is
	# switched off for every DialogBrain agent (best effort); its endpoints stay.
	warning = agent_integration.on_disconnect(doc, client)
	if warning:
		frappe.msgprint(warning, indicator="orange")
	if doc.webhook_id:
		try:
			if client is None:
				raise frappe.ValidationError(_("DialogBrain Settings: base URL and API key are required"))
			_delete_webhook(client, doc.webhook_id)
		except (DialogBrainError, frappe.ValidationError) as exc:
			# Disconnect anyway: the receiver ignores deliveries while disconnected, and
			# the next Connect removes a leftover subscription for the same URL.
			frappe.msgprint(
				_("Could not delete the DialogBrain webhook ({0}); it may keep delivering until removed.").format(
					str(exc)[:200]
				),
				indicator="orange",
			)
	doc.webhook_id = None
	doc.webhook_secret = None
	doc.webhook_status = "disconnected"
	doc.connected = 0
	_server_save(doc)


@frappe.whitelist(methods=["POST"])
def resync() -> None:
	"""Re-run the backfill (dedup'd job; also clears reconcile's failing-thread map)."""
	frappe.only_for(ROLE)
	if not frappe.db.get_single_value(SETTINGS, "connected"):
		frappe.throw(_("Connect to DialogBrain first"))
	enqueue_backfill()


@frappe.whitelist(methods=["POST"])
def set_network_mode(mode: str) -> dict:
	"""``private``: delete the webhook (reconcile polls every minute); ``public``: create it.
	The webhook work happens in ``DialogBrainSettings.validate`` so a plain form save of
	``network_mode`` behaves the same."""
	frappe.only_for(ROLE)
	if mode not in MODES:
		frappe.throw(_("Network mode must be one of: {0}").format(", ".join(MODES)))
	doc = frappe.get_single(SETTINGS)
	result = {"network_mode": mode, "warning": scheduler_warning(mode)}
	if doc.network_mode == mode and (mode == "private" or doc.webhook_id or not doc.connected):
		return result
	if doc.network_mode == mode:  # public but the webhook is missing: recreate it
		configure_webhook(doc, mode)
		_server_save(doc)
		return result
	doc.network_mode = mode
	doc.save(ignore_permissions=True)
	return result


def _failed(delivery) -> bool:
	status = (delivery.get("status") or "").lower()
	if status == DELIVERED:
		return False
	return status not in IN_FLIGHT or (delivery.get("response_status") or 0) >= 400


@frappe.whitelist()
def webhook_health() -> dict:
	"""``{active, last_delivery_at, last_status, failures}``; refreshes ``webhook_status``.

	``failures`` = consecutive failed deliveries, newest first (DialogBrain pauses a
	subscription after 3). A paused (or deleted) subscription is absent from the list.
	"""
	frappe.only_for(ROLE)
	doc = frappe.get_single(SETTINGS)
	if not doc.webhook_id:
		status = PRIVATE_STATUS if doc.network_mode == "private" else "no webhook: run Connect"
		if not doc.connected:
			status = "not connected"
		set_setting("webhook_status", status)
		return {"active": False, "last_delivery_at": None, "last_status": None, "failures": 0}

	client = _client(doc)
	try:
		hooks = client.list_webhooks() or []
		deliveries = client.webhook_deliveries(doc.webhook_id, limit=10) or []
	except DialogBrainError as exc:
		set_setting("webhook_status", f"check failed {now_datetime():%Y-%m-%d %H:%M:%S}: {exc}"[:STATUS_LENGTH])
		frappe.throw(_("Could not check the webhook: {0}").format(str(exc)[:300]))

	active = any(str(h.get("id")) == str(doc.webhook_id) and h.get("active", True) for h in hooks)
	last = deliveries[0] if deliveries else {}
	failures = 0
	for delivery in deliveries:
		if not _failed(delivery):
			break
		failures += 1
	last_at = last.get("delivered_at") or last.get("created_at")
	health = {
		"active": active,
		"last_delivery_at": last_at,
		"last_status": last.get("status"),
		"failures": failures,
	}

	parts = ["active" if active else "PAUSED (inactive at DialogBrain; reconcile re-activates it)"]
	parts.append(f"last delivery {last_at} {last.get('status')}" if last else "no deliveries yet")
	if failures:
		parts.append(f"{failures} recent deliveries failed")
	parts.append(f"checked {now_datetime():%Y-%m-%d %H:%M:%S}")
	set_setting("webhook_status", "; ".join(parts)[:STATUS_LENGTH])
	return health
