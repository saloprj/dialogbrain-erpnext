"""A stand-in for DialogBrain's channels.connect_session.* tools."""

import json

import frappe

from dialogbrain import connect_gate
from dialogbrain.tests.test_permissions import _user

SETTINGS = "DialogBrain Settings"
CLAIM = "DB Channel Claim"
PREFIX = "cc-acc-"


class FakeSessions:
	def __init__(self):
		self.sessions = {}
		self.calls = []
		self.fail = {}  # tool id -> exception
		self.start_answer = {"status": "waiting_scan", "qr": "2@fake-qr-payload", "expires_at": None}
		self.timeout = 20

	def execute(self, tool_id, params, **kwargs):
		self.calls.append((tool_id, dict(params)))
		if tool_id in self.fail:
			raise self.fail[tool_id]
		if tool_id == connect_gate.START:
			sid = f"sess-{len(self.sessions) + 1}"
			self.sessions[sid] = dict(self.start_answer)
			return {"session_id": sid, **self.sessions[sid]}
		sid = params.get("session_id")
		if tool_id in (connect_gate.GET, connect_gate.SUBMIT):
			return dict(self.sessions.get(sid) or {"status": "expired"})
		if tool_id == connect_gate.CANCEL:
			self.sessions[sid] = {"status": "expired"}
			return {"ok": True}
		raise AssertionError(tool_id)

	def of(self, tool_id):
		return [p for t, p in self.calls if t == tool_id]

	def connect(self, sid, account_id, **extra):
		self.sessions[sid] = {"status": "connected", "account_id": account_id, **extra}


def reset(options=None):
	"""A connected site with the feature on, no claims, no test channels, no counters."""
	frappe.set_user("Administrator")
	frappe.db.set_single_value(SETTINGS, "connected", 1, update_modified=False)
	value = connect_gate.DEFAULT if options is None else options
	frappe.db.set_single_value(
		SETTINGS, connect_gate.FIELD, json.dumps(value) if value else None, update_modified=False
	)
	frappe.db.delete(CLAIM)
	for name in frappe.get_all("DB Channel", filters={"name": ["like", PREFIX + "%"]}, pluck="name"):
		frappe.delete_doc("DB Channel", name, force=True, ignore_permissions=True)
	frappe.cache().delete_keys("dialogbrain_rl")


def people():
	return (
		_user("cc-agent@example.com", "Chat Agent"),
		_user("cc-other@example.com", "Chat Agent"),
		_user("cc-manager@example.com", "Chat Manager"),
		_user("cc-plain@example.com", "Accounts User"),
	)


def claim(owner, requested_by=None, channel_type="whatsapp", session_id="sess-1", **extra):
	doc = frappe.get_doc(
		{
			"doctype": CLAIM,
			"requested_by": requested_by or owner or "Administrator",
			"owner_user": owner,
			"channel_type": channel_type,
			"method": "qr",
			"status": "open",
			"session_id": session_id,
			"expires_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), seconds=180),
			**extra,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def allowed(channel):
	return frappe.get_all("DB Channel User", filters={"parent": channel}, pluck="user", order_by="idx")
