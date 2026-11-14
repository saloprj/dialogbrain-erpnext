"""Change a channel's AI-agent mode from ERPNext (Plan 4).

DialogBrain owns the mode; the switch is an ``agents.trigger_update`` on the trigger bound
to the channel (Task 1). ERPNext stores the new mode only after DialogBrain said yes.
Chat Managers and System Managers only.
"""

import re

import frappe
from frappe import _
from frappe.utils import now_datetime

from dialogbrain.client import DialogBrainClient, DialogBrainError
from dialogbrain.permissions import is_manager
from dialogbrain.setup import agent_modes

CHANNEL = "DB Channel"
MODES = {
	"off": {"enabled": False},
	"copilot": {"enabled": True, "send_mode": "draft"},
	"autopilot": {"enabled": True, "send_mode": "auto"},
}
_SECRET = re.compile(r"db_(?:live|test)_\w+")
REASON_MAX = 300


def _require_manager():
	if not is_manager():
		frappe.throw(_("Only a Chat Manager can change the agent mode"), frappe.PermissionError)


def _channel(name):
	if not frappe.db.exists(CHANNEL, name):
		frappe.throw(_("Channel {0} not found").format(name), frappe.DoesNotExistError)
	return frappe.get_doc(CHANNEL, name)


def _reason(exc) -> str:
	if getattr(exc, "error_type", None) == "permission_denied":
		return _("the DialogBrain key needs a member, admin or owner role to change agents")
	return _SECRET.sub("[hidden]", str(exc) or type(exc).__name__)[:REASON_MAX]


def _found(channel, client) -> list:
	try:
		return agent_modes.candidates(client).get(str(channel.account_id), [])
	except DialogBrainError as exc:
		frappe.log_error(title="DialogBrain agent options failed", message=frappe.get_traceback())
		frappe.throw(_("Agents could not be read from DialogBrain: {0}").format(_reason(exc)))


def _payload(name, found) -> dict:
	row = frappe.db.get_value(
		CHANNEL,
		name,
		["agent_mode", "agent_mode_status", "agent_id", "agent_name", "agent_trigger_id"],
		as_dict=True,
	)
	return {
		"mode": row.agent_mode or "",
		"status": row.agent_mode_status or "",
		"agent_id": row.agent_id,
		"agent_name": row.agent_name,
		"trigger_id": row.agent_trigger_id,
		"choices": [
			{k: c[k] for k in ("trigger_id", "agent_name", "mode", "exclusive")}
			for c in found
			if c["exclusive"]
		],
	}


@frappe.whitelist(methods=["POST"])
def agent_options(channel: str) -> dict:
	"""The channel's agent, mode and the agents that could serve it, read live."""
	_require_manager()
	doc = _channel(channel)
	found = _found(doc, DialogBrainClient.from_settings())
	values = agent_modes._decide(doc, found)
	values["agent_mode_synced_at"] = now_datetime()
	frappe.db.set_value(CHANNEL, doc.name, values, update_modified=False)
	return _payload(doc.name, found)


@frappe.whitelist(methods=["POST"])
def choose_agent(channel: str, trigger_id: str) -> dict:
	"""Bind the channel to one of its exclusive agent triggers."""
	_require_manager()
	doc = _channel(channel)
	found = _found(doc, DialogBrainClient.from_settings())
	chosen = next((c for c in found if c["exclusive"] and c["trigger_id"] == str(trigger_id)), None)
	if not chosen:
		frappe.throw(_("This agent trigger can not be chosen for this channel"))
	frappe.db.set_value(
		CHANNEL,
		doc.name,
		{
			"agent_id": chosen["agent_id"],
			"agent_name": chosen["agent_name"],
			"agent_trigger_id": chosen["trigger_id"],
			"agent_mode": chosen["mode"],
			"agent_mode_status": "",
			"agent_mode_synced_at": now_datetime(),
		},
		update_modified=False,
	)
	return _payload(doc.name, found)


@frappe.whitelist(methods=["POST"])
def set_agent_mode(channel: str, mode: str) -> dict:
	"""Switch the channel's agent to copilot (drafts), autopilot (answers) or off."""
	_require_manager()
	if mode not in MODES:
		frappe.throw(_("Unknown agent mode: {0}").format(mode))
	doc = _channel(channel)
	if not (doc.agent_trigger_id and doc.agent_id):
		frappe.throw(_("This channel has no agent to switch. Open the channel and choose one."))
	client = DialogBrainClient.from_settings()
	try:
		found = agent_modes.candidates(client).get(str(doc.account_id), [])
	except DialogBrainError as exc:
		frappe.log_error(title="DialogBrain agent options failed", message=frappe.get_traceback())
		frappe.throw(_("DialogBrain did not change the agent mode: {0}").format(_reason(exc)))
	bound = str(doc.agent_trigger_id)
	if not any(c["exclusive"] and c["trigger_id"] == bound for c in found):
		# the stored binding is stale (trigger shared, deleted, agent inactive): never switch it
		values = agent_modes._decide(doc, found)
		values["agent_mode_synced_at"] = now_datetime()
		frappe.db.set_value(CHANNEL, doc.name, values, update_modified=False)
		frappe.db.commit()  # frappe.throw rolls back; keep the refreshed binding
		frappe.throw(
			_("The agent setup of this channel changed in DialogBrain; the channel was refreshed. Nothing was switched.")
		)
	params = {"trigger_id": int(doc.agent_trigger_id), "agent_id": int(doc.agent_id), **MODES[mode]}
	try:
		client.execute("agents.trigger_update", params)
	except DialogBrainError as exc:
		frappe.log_error(title="DialogBrain agents.trigger_update failed", message=frappe.get_traceback())
		frappe.throw(_("DialogBrain did not change the agent mode: {0}").format(_reason(exc)))
	frappe.db.set_value(
		CHANNEL, doc.name, {"agent_mode": mode, "agent_mode_synced_at": now_datetime()}, update_modified=False
	)
	return _payload(doc.name, found)


@frappe.whitelist(methods=["POST"])
def refresh_agent_modes() -> dict:
	"""Read every channel's agent and mode from DialogBrain again."""
	_require_manager()
	try:
		return agent_modes.sync()
	except DialogBrainError as exc:
		frappe.db.commit()  # keep the reason sync() stored on the channels
		frappe.throw(_("Agents could not be read from DialogBrain: {0}").format(_reason(exc)))
