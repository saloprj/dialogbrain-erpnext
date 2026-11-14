"""Read each channel's AI-agent trigger from DialogBrain and store the channel's mode (Plan 4).

DialogBrain owns the mode: an ``incoming_message`` trigger of an active agent says which
channel accounts it serves and whether the agent answers by itself (``auto``) or leaves a
draft. ERPNext only reads it (``agents.list`` + ``agents.get``) and stores the result on
DB Channel; the fields are read-only in the form.

``autopilot`` means "the trigger lets the agent answer by itself". DialogBrain's safety
gate can still turn a single answer into a draft; ERPNext does not see that.
"""

import frappe
from frappe import _
from frappe.utils import now_datetime

from dialogbrain.client import DialogBrainClient, DialogBrainError

CHANNEL = "DB Channel"
TRIGGER_TYPE = "incoming_message"
BOUND = ("agent_id", "agent_name", "agent_trigger_id")


def _ids(trigger: dict) -> list:
	conditions = trigger.get("conditions") or {}
	return [str(i) for i in (conditions.get("channel_account_ids") or [])]


def mode_of(trigger: dict, agent: dict) -> str:
	"""``off`` when the trigger is disabled; ``autopilot`` when its send mode (the agent's
	when the trigger has none) is ``auto``; else ``copilot``."""
	if not trigger.get("enabled"):
		return "off"
	send_mode = trigger.get("send_mode")
	if send_mode is None:
		send_mode = agent.get("send_mode") or (agent.get("execution_config") or {}).get("default_send_mode")
	return "autopilot" if send_mode == "auto" else "copilot"


def candidates(client) -> dict:
	"""``{account_id: [{agent_id, agent_name, trigger_id, mode, exclusive}]}`` for the
	incoming-message triggers of active, non-template agents."""
	return _read(client)[0]


def _read(client) -> tuple:
	"""``(candidates, failed_agent_ids)``. An ``agents.get`` that fails (a 5xx, an agent
	deleted since the list) skips that agent only; ``permission_denied`` and a failing
	``agents.list`` raise: nothing can be read."""
	found = {}
	failed = set()
	for listed in (client.execute("agents.list", {}) or {}).get("agents") or []:
		if listed.get("is_template") or listed.get("status") != "active":
			continue
		try:
			agent = client.execute("agents.get", {"agent_id": listed.get("id")}) or {}
		except DialogBrainError as exc:
			if exc.error_type == "permission_denied":
				raise
			failed.add(str(listed.get("id")))
			continue
		for trigger in agent.get("triggers") or []:
			if trigger.get("trigger_type") != TRIGGER_TYPE:
				continue
			ids = _ids(trigger)
			for account_id in ids:
				found.setdefault(account_id, []).append(
					{
						"agent_id": str(listed.get("id")),
						"agent_name": listed.get("name") or agent.get("name"),
						"trigger_id": str(trigger.get("id")),
						"mode": mode_of(trigger, agent),
						"exclusive": len(ids) == 1,
					}
				)
	return found, failed


def _failure_text(exc: DialogBrainError) -> str:
	if exc.error_type == "permission_denied":
		return _("The DialogBrain key may not read agents: connect with a member, admin or owner key")
	return _("Agents could not be read from DialogBrain: {0}").format(str(exc)[:300])


def _unbound() -> dict:
	return {"agent_id": None, "agent_name": None, "agent_trigger_id": None}


def _decide(channel, found: list) -> dict:
	"""The values to store for one channel (rules 2-6 of the Plan 4 spec)."""
	exclusive = [c for c in found if c["exclusive"]]
	chosen = None
	bound = str(channel.agent_trigger_id or "")
	if bound:
		chosen = next((c for c in exclusive if c["trigger_id"] == bound), None)
	if chosen is None and len(exclusive) == 1:
		chosen = exclusive[0]
	if chosen:
		return {
			"agent_id": chosen["agent_id"],
			"agent_name": chosen["agent_name"],
			"agent_trigger_id": chosen["trigger_id"],
			"agent_mode": chosen["mode"],
			"agent_mode_status": "",
		}
	if exclusive:
		status = _("Several agents answer this channel: choose one")
	elif found:
		status = _("The agent trigger also serves other channels: change it in DialogBrain")
	else:
		status = _("No agent answers this channel in DialogBrain")
	return {**_unbound(), "agent_mode": "", "agent_mode_status": status}


def sync(client=None) -> dict:
	"""Bind every DB Channel to its agent trigger. Returns ``{linked, ambiguous, none}``.

	When DialogBrain cannot be read, the reason goes to every channel's
	``agent_mode_status`` (bindings and modes stay as they are) and the error is raised."""
	client = client or DialogBrainClient.from_settings()
	try:
		found, failed = _read(client)
	except DialogBrainError as exc:
		frappe.db.sql(
			"update `tabDB Channel` set agent_mode_status = %s", (_failure_text(exc)[:500],)
		)
		raise
	counts = {"linked": 0, "ambiguous": 0, "none": 0}
	now = now_datetime()
	channels = frappe.get_all(
		CHANNEL, fields=["name", "account_id", "agent_id", "agent_trigger_id", "agent_mode"], limit_page_length=0
	)
	for channel in channels:
		if channel.agent_id and str(channel.agent_id) in failed:
			continue  # its agent could not be read this run: leave it as stored
		values = _decide(channel, found.get(str(channel.account_id), []))
		if values["agent_trigger_id"]:
			counts["linked"] += 1
		elif found.get(str(channel.account_id)):
			counts["ambiguous"] += 1
		else:
			counts["none"] += 1
		values["agent_mode_synced_at"] = now
		frappe.db.set_value(CHANNEL, channel.name, values, update_modified=False)
	return counts


def hourly() -> None:
	"""Scheduler entry: a mode changed in DialogBrain shows up within the hour. A site
	that is not connected raises in ``from_settings``: that is just another error here."""
	try:
		sync()
	except Exception:
		frappe.log_error(title="DialogBrain: hourly agent mode sync failed")
