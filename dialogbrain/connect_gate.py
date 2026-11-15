"""Whether (and how) this site's DialogBrain can connect a channel from ERPNext.

The answer is stored in ``DialogBrain Settings.connect_channel_options`` (JSON,
``{channel: [methods]}``; empty: not supported) and refreshed at Connect and every hour,
so the "Connect channel" button appears by itself once DialogBrain ships the tools.

DialogBrain's tool catalog lists tool ids only, no parameter schemas. Which channels and
methods work comes from the optional tool ``channels.connect_session.options``; without
it only the QR flows of WhatsApp and Telegram are offered.
"""

import json

import frappe

from dialogbrain.client import DialogBrainClient, DialogBrainError

SETTINGS = "DialogBrain Settings"
FIELD = "connect_channel_options"
START = "channels.connect_session.start"
GET = "channels.connect_session.get"
SUBMIT = "channels.connect_session.submit"
CANCEL = "channels.connect_session.cancel"
OPTIONS = "channels.connect_session.options"
NEEDED = {START, GET, SUBMIT, CANCEL}
METHODS = ("qr", "pairing_code", "phone")
DEFAULT = {"whatsapp": ["qr"], "telegram": ["qr"]}


def _clean(channels) -> dict:
	"""Only channel names and the methods this app knows how to drive."""
	out = {}
	if not isinstance(channels, dict):
		return out
	for channel, methods in channels.items():
		if not isinstance(channel, str) or not isinstance(methods, (list, tuple)):
			continue
		known = [m for m in METHODS if m in methods]
		if known:
			out[channel.strip().lower()[:40]] = known
	return out


def read(client, tools=None) -> "dict | None":
	"""Ask DialogBrain. ``None``: the tools are not there. Raises ``DialogBrainError``."""
	tools = set(client.list_tools() or ()) if tools is None else set(tools)
	if not NEEDED <= tools:
		return None
	if OPTIONS not in tools:
		return dict(DEFAULT)
	try:
		answer = client.execute(OPTIONS, {}) or {}
	except DialogBrainError:
		return dict(DEFAULT)
	return _clean(answer.get("channels")) or dict(DEFAULT)


def stored_value(client, tools=None) -> "str | None":
	"""The value for the Settings field (JSON text or None); never raises."""
	try:
		found = read(client, tools)
	except Exception:
		return frappe.db.get_single_value(SETTINGS, FIELD) or None
	return json.dumps(found, sort_keys=True) if found else None


def refresh(client=None) -> "dict | None":
	"""Re-read and store. A failing read (DialogBrain down, no key) keeps the previous value:
	the hourly agent-mode sync already reports an unreachable DialogBrain."""
	try:
		client = client or DialogBrainClient.from_settings()
		found = read(client)
	except Exception:
		return options()
	frappe.db.set_single_value(
		SETTINGS, FIELD, json.dumps(found, sort_keys=True) if found else None, update_modified=False
	)
	return found


def options() -> "dict | None":
	"""``{channel: [methods]}``, or ``None`` when the feature is off or the site is not connected."""
	row = frappe.db.get_value(SETTINGS, SETTINGS, ["connected", FIELD], as_dict=True) or {}
	if str(row.get("connected") or "0") in ("0", ""):
		return None
	try:
		found = json.loads(row.get(FIELD) or "null")
	except ValueError:
		return None
	return _clean(found) or None
