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

from dialogbrain import compat
from dialogbrain.client import DialogBrainClient, DialogBrainError

CHANNEL = "DB Channel"
SETTINGS = "DialogBrain Settings"
TRIGGER_TYPE = "incoming_message"


def _ids(trigger: dict) -> list:
	conditions = trigger.get("conditions") or {}
	return [str(i) for i in (conditions.get("channel_account_ids") or [])]


def _types(trigger: dict) -> list:
	conditions = trigger.get("conditions") or {}
	types = conditions.get("channel_types") or []
	return [str(t) for t in types] if isinstance(types, list) else []


def _serves_type(types: list, channel_type) -> bool:
	"""Whether a trigger limited to ``types`` (empty: no limit) can answer a channel of
	``channel_type`` (compared case-insensitively). When unsure (unknown channel type, a related type such as
	``telegram`` / ``telegram_bot``) it counts as serving: a false warning is harmless, a
	missing one makes "Off" look like off when it is not."""
	if not types or not channel_type:
		return True
	ct = str(channel_type).lower()
	lowered = [str(t).lower() for t in types]
	return any(t == ct or ct.startswith(t + "_") or t.startswith(ct + "_") for t in lowered)


def _another():
	return _("Another agent trigger also answers this channel: Off here does not stop it")


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
	"""``(by_account, failed_agent_ids, general)``.

	``by_account``: candidates of triggers that list channel account ids. ``general``:
	``(channel_types, candidate)`` for each ENABLED trigger without channel ids (a catch-all,
	or one limited only by ``channel_types``): it answers every channel of those types, is
	never exclusive and never bound. An ``agents.get`` that fails (a 5xx, an agent deleted
	since the list) or answers without a ``triggers`` list skips that agent only;
	``permission_denied``, a failing ``agents.list`` and an ``agents.list`` answer that is not
	``{"agents": [...]}`` raise: nothing can be read, nothing is rewritten."""
	found = {}
	failed = set()
	general = []
	answer = client.execute("agents.list", {})
	if not isinstance(answer, dict) or not isinstance(answer.get("agents"), list):
		raise DialogBrainError("unexpected answer to agents.list")
	for listed in answer["agents"]:
		if not isinstance(listed, dict) or listed.get("is_template") or listed.get("status") != "active":
			continue
		try:
			agent = client.execute("agents.get", {"agent_id": listed.get("id")})
		except DialogBrainError as exc:
			if exc.error_type == "permission_denied":
				raise
			failed.add(str(listed.get("id")))
			continue
		if not isinstance(agent, dict) or not isinstance(agent.get("triggers"), list):
			failed.add(str(listed.get("id")))
			continue
		for trigger in agent["triggers"]:
			if not isinstance(trigger, dict) or trigger.get("trigger_type") != TRIGGER_TYPE:
				continue
			ids = _ids(trigger)

			def candidate(exclusive, trigger=trigger):
				return {
					"agent_id": str(listed.get("id")),
					"agent_name": listed.get("name") or agent.get("name"),
					"trigger_id": str(trigger.get("id")),
					"mode": mode_of(trigger, agent),
					"exclusive": exclusive,
				}

			if not ids:
				if trigger.get("enabled"):
					general.append((_types(trigger), candidate(False)))
				continue
			for account_id in ids:
				found.setdefault(account_id, []).append(candidate(len(ids) == 1))
	return found, failed, general


def _for_channel(found: dict, general: list, channel) -> list:
	"""Every candidate that answers ``channel``: its own triggers, then the catch-alls."""
	own = list(found.get(str(channel.account_id), []))
	return own + [dict(c) for types, c in general if _serves_type(types, channel.get("channel_type"))]


def read_channel(client, channel) -> tuple:
	"""Live read for one channel: ``(found, unreadable)``. ``unreadable`` is True when the
	agent the channel is bound to could not be read in this call (a 5xx, a deletion in
	between): the caller must then write nothing for the channel, as ``sync`` does."""
	found, failed, general = _read(client)
	unreadable = bool(channel.agent_id) and str(channel.agent_id) in failed
	return _for_channel(found, general, channel), unreadable


def store_decision(channel, found: list) -> None:
	"""Store what ``sync`` would store for this one channel, given its live candidates."""
	values = _decide(channel, found)
	values["agent_mode_synced_at"] = now_datetime()
	frappe.db.set_value(CHANNEL, channel.name, values, update_modified=False)


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
		# another enabled trigger (exclusive, shared or catch-all) answers this channel too:
		# switching the bound one is real, but "Off" here does not silence the channel
		others = [c for c in found if c["trigger_id"] != chosen["trigger_id"] and c["mode"] != "off"]
		return {
			"agent_id": chosen["agent_id"],
			"agent_name": chosen["agent_name"],
			"agent_trigger_id": chosen["trigger_id"],
			"agent_mode": chosen["mode"],
			"agent_mode_status": _another() if others else "",
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
		found, failed, general = _read(client)
	except DialogBrainError as exc:
		frappe.db.sql(
			"update `tabDB Channel` set agent_mode_status = %s", (_failure_text(exc)[:500],)
		)
		raise
	counts = {"linked": 0, "ambiguous": 0, "none": 0}
	now = now_datetime()
	channels = frappe.get_all(
		CHANNEL, fields=["name", "account_id", "channel_type", "agent_id", "agent_trigger_id"], limit_page_length=0
	)
	for channel in channels:
		if channel.agent_id and str(channel.agent_id) in failed:
			continue  # its agent could not be read this run: leave it as stored
		serving = _for_channel(found, general, channel)
		values = _decide(channel, serving)
		if values["agent_trigger_id"]:
			counts["linked"] += 1
		elif serving:
			counts["ambiguous"] += 1
		else:
			counts["none"] += 1
		values["agent_mode_synced_at"] = now
		frappe.db.set_value(CHANNEL, channel.name, values, update_modified=False)
	return counts


def hourly() -> None:
	"""Scheduler entry: a mode changed in DialogBrain shows up within the hour. A site that
	is not connected is skipped (read typed: on v15 a Single's ``"0"`` is a truthy string)."""
	if not compat.single_values(SETTINGS, ["connected"]).connected:
		return
	try:
		sync()
	except Exception:
		frappe.log_error(title="DialogBrain: hourly agent mode sync failed")
