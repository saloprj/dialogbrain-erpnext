"""Connect a channel from ERPNext, Task 1: the feature gate."""

import json
from unittest.mock import patch

import frappe

from dialogbrain import connect_gate
from dialogbrain.client import DialogBrainError
from dialogbrain.setup import agent_modes
from dialogbrain.tests.utils import DialogBrainTestCase

SETTINGS = "DialogBrain Settings"


class FakeGateClient:
	def __init__(self, tools, channels=None, fail_catalog=False):
		self.tools = set(tools)
		self.channels = channels
		self.fail_catalog = fail_catalog
		self.calls = []

	def list_tools(self):
		if self.fail_catalog:
			raise DialogBrainError("catalog down")
		return set(self.tools)

	def execute(self, tool_id, params, **kwargs):
		self.calls.append((tool_id, params))
		if tool_id == connect_gate.OPTIONS:
			return {"channels": self.channels}
		raise AssertionError(tool_id)


def _set(connected=1, value=None):
	frappe.db.set_single_value(SETTINGS, "connected", connected, update_modified=False)
	frappe.db.set_single_value(SETTINGS, connect_gate.FIELD, value, update_modified=False)


class TestConnectGate(DialogBrainTestCase):
	def setUp(self):
		_set()

	def test_off_without_start_tool(self):
		client = FakeGateClient({"channels.list", connect_gate.GET})
		self.assertIsNone(connect_gate.refresh(client))
		self.assertIsNone(connect_gate.options())
		self.assertEqual(client.calls, [])

	def test_default_without_options_tool(self):
		connect_gate.refresh(FakeGateClient(connect_gate.NEEDED))
		self.assertEqual(connect_gate.options(), connect_gate.DEFAULT)

	def test_options_tool_controls_methods(self):
		client = FakeGateClient(
			connect_gate.NEEDED | {connect_gate.OPTIONS},
			{"whatsapp": ["qr", "pairing_code", "carrier_pigeon"], "viber": ["qr"], "fax": ["smoke"],
			 "<img src=x onerror=alert(1)>": ["qr"]},
		)
		connect_gate.refresh(client)
		self.assertEqual(connect_gate.options(), {"whatsapp": ["qr", "pairing_code"], "viber": ["qr"]})

	def test_refresh_failure_keeps_previous(self):
		_set(value=json.dumps({"telegram": ["qr", "phone"]}))
		connect_gate.refresh(FakeGateClient(set(), fail_catalog=True))
		self.assertEqual(connect_gate.options(), {"telegram": ["qr", "phone"]})

	def test_off_when_not_connected(self):
		_set(connected=0, value=json.dumps(connect_gate.DEFAULT))
		self.assertIsNone(connect_gate.options())

	def test_hourly_refreshes_gate(self):
		client = FakeGateClient(connect_gate.NEEDED)
		with patch.object(agent_modes, "sync"), patch(
			"dialogbrain.connect_gate.DialogBrainClient.from_settings", return_value=client
		):
			agent_modes.hourly()
		self.assertEqual(connect_gate.options(), connect_gate.DEFAULT)
