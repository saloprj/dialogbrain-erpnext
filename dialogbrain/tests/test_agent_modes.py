"""Plan 4 Task 1: each channel's agent trigger and mode are read from DialogBrain."""

import copy
from unittest.mock import patch

import frappe

from dialogbrain.client import DialogBrainError
from dialogbrain.setup import agent_modes
from dialogbrain.tests.test_agent_integration import FakeDialogBrain, _AgentIntegrationCase
from dialogbrain.tests.test_permissions import _user
from dialogbrain.tests.utils import DialogBrainTestCase

SEVERAL = "Several agents answer this channel: choose one"
SHARED = "The agent trigger also serves other channels: change it in DialogBrain"
NOBODY = "No agent answers this channel in DialogBrain"
DENIED = "The DialogBrain key may not read agents: connect with a member, admin or owner key"


def _trigger(tid, ids, send_mode=None, enabled=True, kind="incoming_message", conditions=None):
	if conditions is None:
		conditions = {"channel_account_ids": ids}
	return {
		"id": tid,
		"trigger_type": kind,
		"conditions": conditions,
		"send_mode": send_mode,
		"enabled": enabled,
		"priority": 1,
	}


def _agents():
	return {
		10: {
			"id": 10,
			"name": "Sales",
			"status": "active",
			"send_mode": "draft",
			"triggers": [
				_trigger(1, ["101"]),
				_trigger(2, ["102"], "auto"),
				_trigger(3, ["103"], "auto", enabled=False),
				_trigger(5, ["104"], "auto"),
			],
		},
		11: {
			"id": 11,
			"name": "Support",
			"status": "active",
			"send_mode": "draft",
			"triggers": [
				_trigger(4, [104], "draft"),
				_trigger(6, ["105", "106"], "auto"),
				_trigger(7, None, "auto", conditions={}),
				_trigger(9, ["110"], "auto", kind="schedule"),
			],
		},
		12: {
			"id": 12,
			"name": "Old",
			"status": "paused",
			"send_mode": "draft",
			"triggers": [_trigger(8, ["107"])],
		},
		13: {
			"id": 13,
			"name": "Template",
			"status": "active",
			"is_template": True,
			"send_mode": "auto",
			"triggers": [_trigger(20, ["108"], "auto")],
		},
	}


class FakeAgents:
	def __init__(self):
		self.agents = _agents()
		self.calls = []
		self.error = None
		self.get_errors = {}
		self.fail = {}

	def execute(self, tool, params):
		self.calls.append((tool, copy.deepcopy(params)))
		if self.error:
			raise self.error
		if tool == "agents.get" and params["agent_id"] in self.get_errors:
			raise self.get_errors[params["agent_id"]]
		if tool == "agents.list":
			rows = [
				{
					"id": a["id"],
					"name": a["name"],
					"status": a["status"],
					"send_mode": a["send_mode"],
					"trigger_count": len(a["triggers"]),
					"is_template": bool(a.get("is_template")),
				}
				for a in self.agents.values()
			]
			return {"agents": rows}
		if tool == "agents.get":
			return copy.deepcopy(self.agents[params["agent_id"]])
		if tool == "agents.trigger_update":
			if tool in self.fail:
				raise self.fail[tool]
			agent = self.agents[params["agent_id"]]
			trigger = next(t for t in agent["triggers"] if t["id"] == params["trigger_id"])
			for key in ("enabled", "send_mode"):
				if key in params:
					trigger[key] = params[key]
			return {"id": trigger["id"], "enabled": trigger["enabled"], "send_mode": trigger["send_mode"]}
		raise AssertionError(tool)


class _Base(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete("DB Channel")
		for n in range(101, 111):
			frappe.get_doc(
				{"doctype": "DB Channel", "account_id": str(n), "channel_type": "telegram", "title": f"Ch {n}"}
			).insert(ignore_permissions=True)
		self.fake = FakeAgents()

	def row(self, account_id):
		return frappe.db.get_value(
			"DB Channel",
			str(account_id),
			["agent_id", "agent_name", "agent_trigger_id", "agent_mode", "agent_mode_status", "agent_mode_synced_at"],
			as_dict=True,
		)


class TestAgentModes(_Base):
	def test_modes_are_read_from_the_exclusive_trigger(self):
		counts = agent_modes.sync(self.fake)
		for account_id, trigger, mode in (("101", "1", "copilot"), ("102", "2", "autopilot"), ("103", "3", "off")):
			row = self.row(account_id)
			self.assertEqual((row.agent_trigger_id, row.agent_mode), (trigger, mode))
			self.assertEqual((row.agent_id, row.agent_name), ("10", "Sales"))
			self.assertFalse(row.agent_mode_status)
			self.assertIsNotNone(row.agent_mode_synced_at)
		self.assertEqual(counts, {"linked": 3, "ambiguous": 3, "none": 4})

	def test_two_exclusive_agents_on_one_channel_are_not_guessed(self):
		agent_modes.sync(self.fake)
		row = self.row("104")
		self.assertEqual((row.agent_mode, row.agent_mode_status), ("", SEVERAL))
		self.assertFalse(row.agent_trigger_id)

	def test_a_shared_trigger_is_shown_and_not_bound(self):
		agent_modes.sync(self.fake)
		for account_id in ("105", "106"):
			row = self.row(account_id)
			self.assertEqual((row.agent_mode, row.agent_mode_status), ("", SHARED))
			self.assertFalse(row.agent_trigger_id)

	def test_a_channel_nobody_answers(self):
		agent_modes.sync(self.fake)
		for account_id in ("107", "108", "110"):  # paused agent, template, non-message trigger
			row = self.row(account_id)
			self.assertEqual((row.agent_mode, row.agent_mode_status), ("", NOBODY))
			self.assertFalse(row.agent_trigger_id)

	def test_a_bound_trigger_survives_a_second_agent_appearing(self):
		frappe.db.set_value(
			"DB Channel", "104", {"agent_id": "11", "agent_name": "Support", "agent_trigger_id": "4"}
		)
		agent_modes.sync(self.fake)
		row = self.row("104")
		self.assertEqual((row.agent_trigger_id, row.agent_id, row.agent_mode), ("4", "11", "copilot"))
		self.assertFalse(row.agent_mode_status)

	def test_a_bound_trigger_survives_and_its_mode_is_refreshed(self):
		agent_modes.sync(self.fake)
		self.fake.agents[10]["triggers"][0]["send_mode"] = "auto"
		agent_modes.sync(self.fake)
		self.assertEqual(
			(self.row("101").agent_trigger_id, self.row("101").agent_mode), ("1", "autopilot")
		)

	def test_a_bound_trigger_that_is_gone_is_unbound(self):
		agent_modes.sync(self.fake)
		self.assertEqual(self.row("101").agent_trigger_id, "1")
		self.fake.agents[10]["triggers"] = [t for t in self.fake.agents[10]["triggers"] if t["id"] != 1]
		agent_modes.sync(self.fake)
		row = self.row("101")
		self.assertEqual((row.agent_mode, row.agent_mode_status), ("", NOBODY))
		self.assertFalse(row.agent_trigger_id or row.agent_id or row.agent_name)

	def test_a_bound_trigger_that_became_shared_is_unbound(self):
		agent_modes.sync(self.fake)
		self.assertEqual(self.row("101").agent_trigger_id, "1")
		self.fake.agents[10]["triggers"][0]["conditions"]["channel_account_ids"] = ["101", "109"]
		exclusive_101 = [
			t
			for a in self.fake.agents.values()
			for t in a["triggers"]
			if t["conditions"].get("channel_account_ids") == ["101"]
		]
		self.assertEqual(exclusive_101, [])
		agent_modes.sync(self.fake)
		row = self.row("101")
		self.assertEqual((row.agent_mode, row.agent_mode_status), ("", SHARED))
		self.assertFalse(row.agent_trigger_id or row.agent_id or row.agent_name)

	def test_system_templates_are_ignored(self):
		agent_modes.sync(self.fake)
		self.assertEqual(self.row("108").agent_mode_status, NOBODY)
		self.assertNotIn(("agents.get", {"agent_id": 13}), self.fake.calls)

	def test_account_ids_compare_as_strings(self):
		found = agent_modes.candidates(self.fake)
		self.assertIn("4", {c["trigger_id"] for c in found["104"]})  # trigger 4 lists the int 104
		self.assertEqual(len(found["104"]), 2)
		self.assertTrue(all(c["exclusive"] for c in found["104"]))

	def test_mode_of(self):
		agent = {"send_mode": "draft"}
		self.assertEqual(agent_modes.mode_of({"enabled": False, "send_mode": "auto"}, agent), "off")
		self.assertEqual(agent_modes.mode_of({"enabled": True, "send_mode": None}, agent), "copilot")
		self.assertEqual(agent_modes.mode_of({"enabled": True, "send_mode": None}, {"send_mode": "auto"}), "autopilot")
		self.assertEqual(
			agent_modes.mode_of(
				{"enabled": True, "send_mode": None}, {"execution_config": {"default_send_mode": "auto"}}
			),
			"autopilot",
		)
		self.assertEqual(agent_modes.mode_of({"enabled": True, "send_mode": "draft"}, {"send_mode": "auto"}), "copilot")

	def test_a_key_that_may_not_read_agents(self):
		agent_modes.sync(self.fake)
		self.fake.error = DialogBrainError("no", error_type="permission_denied")
		with self.assertRaises(DialogBrainError):
			agent_modes.sync(self.fake)
		for n in range(101, 111):
			self.assertEqual(self.row(n).agent_mode_status, DENIED)
		row = self.row("102")  # binding and mode untouched
		self.assertEqual((row.agent_trigger_id, row.agent_mode), ("2", "autopilot"))

	def test_one_agent_failing_leaves_its_channels_and_refreshes_the_rest(self):
		agent_modes.sync(self.fake)
		stored = self.row("101")
		self.fake.get_errors[10] = DialogBrainError("HTTP 500", status=500)
		self.fake.agents[11]["triggers"][0]["send_mode"] = "auto"  # trigger 4 -> autopilot
		frappe.db.set_value("DB Channel", "104", {"agent_id": "11", "agent_name": "Support", "agent_trigger_id": "4"})
		frappe.db.set_value("DB Channel", "102", "agent_mode_status", "kept")
		agent_modes.sync(self.fake)
		self.assertEqual(self.row("101"), stored)  # bound to the failed agent: untouched
		self.assertEqual(self.row("102").agent_mode_status, "kept")
		self.assertEqual(self.row("102").agent_mode, "autopilot")
		row = self.row("104")  # agent 11 read: refreshed (trigger 5 of agent 10 is unknown now)
		self.assertEqual((row.agent_trigger_id, row.agent_mode), ("4", "autopilot"))
		self.assertEqual(self.row("105").agent_mode_status, SHARED)

	def test_a_failing_agents_get_with_permission_denied_reads_nothing(self):
		agent_modes.sync(self.fake)
		self.fake.get_errors[10] = DialogBrainError("no", error_type="permission_denied")
		with self.assertRaises(DialogBrainError):
			agent_modes.sync(self.fake)
		for n in range(101, 111):
			self.assertEqual(self.row(n).agent_mode_status, DENIED)
		self.assertEqual(self.row("102").agent_trigger_id, "2")

	def test_hourly_swallows_errors_and_skips_a_disconnected_site(self):
		with patch("dialogbrain.setup.agent_modes.DialogBrainClient.from_settings") as mk:
			mk.side_effect = DialogBrainError("DialogBrain Settings: base URL and API key are required")
			agent_modes.hourly()  # no raise
			mk.side_effect = None
			mk.return_value = self.fake
			agent_modes.hourly()
		self.assertEqual(self.row("102").agent_mode, "autopilot")
		self.fake.error = RuntimeError("boom")
		with patch("dialogbrain.setup.agent_modes.DialogBrainClient.from_settings", return_value=self.fake):
			agent_modes.hourly()  # no raise


class _FailingList(FakeDialogBrain):
	def execute(self, tool_id, params, thread_id=None, request_id=None):
		if tool_id == "agents.list":
			raise DialogBrainError("denied", error_type="permission_denied")
		return super().execute(tool_id, params, thread_id, request_id)


class TestConnectSurvivesAgentModes(_AgentIntegrationCase):
	def test_connect_survives_agents_list_failing(self):
		frappe.db.delete("DB Channel")
		result = self._connect(_FailingList())
		self.assertTrue(result["channels"])
		statuses = frappe.get_all("DB Channel", pluck="agent_mode_status")
		self.assertTrue(statuses)
		self.assertEqual(set(statuses), {DENIED})
		self.assertTrue(frappe.db.get_single_value("DialogBrain Settings", "connected"))


class TestChangeMode(_Base):
	def setUp(self):
		super().setUp()
		agent_modes.sync(self.fake)  # 101 -> trigger 1 (copilot), 102 -> trigger 2 (autopilot)
		self.manager = _user("mode-manager@example.com", "Chat Manager")
		self.agent = _user("mode-agent@example.com", "Chat Agent")
		patcher = patch("dialogbrain.api.channels.DialogBrainClient.from_settings", return_value=self.fake)
		patcher.start()
		self.addCleanup(patcher.stop)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user(self.manager)

	def updates(self):
		return [c for c in self.fake.calls if c[0] == "agents.trigger_update"]

	def test_set_mode_sends_the_trigger_update_and_stores_the_mode(self):
		from dialogbrain.api import channels

		result = channels.set_agent_mode("101", "autopilot")
		self.assertEqual(
			self.updates(),
			[("agents.trigger_update", {"trigger_id": 1, "agent_id": 10, "enabled": True, "send_mode": "auto"})],
		)
		self.assertEqual(self.row("101").agent_mode, "autopilot")
		self.assertIsNotNone(self.row("101").agent_mode_synced_at)
		self.assertEqual(result["mode"], "autopilot")
		self.assertEqual(
			(result["agent_id"], result["agent_name"], result["trigger_id"]), ("10", "Sales", "1")
		)
		self.fake.calls.clear()
		channels.set_agent_mode("101", "copilot")
		self.assertEqual(
			self.updates(),
			[("agents.trigger_update", {"trigger_id": 1, "agent_id": 10, "enabled": True, "send_mode": "draft"})],
		)
		self.assertEqual(self.row("101").agent_mode, "copilot")

	def test_off_disables_the_trigger_and_autopilot_enables_it_again(self):
		from dialogbrain.api import channels

		channels.set_agent_mode("102", "off")
		self.assertEqual(
			self.updates()[-1][1], {"trigger_id": 2, "agent_id": 10, "enabled": False}
		)
		self.assertEqual(self.row("102").agent_mode, "off")
		self.assertFalse(self.fake.agents[10]["triggers"][1]["enabled"])
		channels.set_agent_mode("102", "autopilot")
		self.assertEqual(
			self.updates()[-1][1], {"trigger_id": 2, "agent_id": 10, "enabled": True, "send_mode": "auto"}
		)
		self.assertEqual(self.row("102").agent_mode, "autopilot")
		self.assertTrue(self.fake.agents[10]["triggers"][1]["enabled"])

	def test_dialogbrain_refusing_keeps_the_old_mode(self):
		from dialogbrain.api import channels

		before = self.row("101")
		self.fake.fail["agents.trigger_update"] = DialogBrainError(
			"agent not found for key db_live_abc123SECRET", error_type="not_found"
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			channels.set_agent_mode("101", "off")
		message = str(ctx.exception)
		self.assertIn("DialogBrain did not change the agent mode", message)
		self.assertIn("agent not found", message)
		self.assertNotIn("abc123SECRET", message)
		self.assertEqual(self.row("101"), before)

	def test_a_bound_trigger_that_became_shared_is_not_switched(self):
		from dialogbrain.api import channels

		self.fake.agents[10]["triggers"][0]["conditions"]["channel_account_ids"] = ["101", "109"]
		with self.assertRaises(frappe.ValidationError) as ctx:
			channels.set_agent_mode("101", "off")
		self.assertIn("changed in DialogBrain", str(ctx.exception))
		self.assertEqual(self.updates(), [])
		row = self.row("101")
		self.assertEqual((row.agent_mode, row.agent_mode_status), ("", SHARED))
		self.assertFalse(row.agent_trigger_id or row.agent_id or row.agent_name)

	def test_a_bound_trigger_that_was_deleted_is_not_switched(self):
		from dialogbrain.api import channels

		self.fake.agents[10]["triggers"] = [t for t in self.fake.agents[10]["triggers"] if t["id"] != 1]
		with self.assertRaises(frappe.ValidationError):
			channels.set_agent_mode("101", "autopilot")
		self.assertEqual(self.updates(), [])
		row = self.row("101")
		self.assertEqual((row.agent_mode, row.agent_mode_status), ("", NOBODY))
		self.assertFalse(row.agent_trigger_id)

	def test_a_failing_live_read_keeps_everything(self):
		from dialogbrain.api import channels

		before = self.row("101")
		self.fake.error = DialogBrainError("HTTP 500 db_live_abc123SECRET", status=500)
		with self.assertRaises(frappe.ValidationError) as ctx:
			channels.set_agent_mode("101", "off")
		self.assertIn("DialogBrain did not change the agent mode", str(ctx.exception))
		self.assertNotIn("abc123SECRET", str(ctx.exception))
		self.assertEqual(self.updates(), [])
		self.assertEqual(self.row("101"), before)

	def test_set_mode_returns_the_real_choices(self):
		from dialogbrain.api import channels

		result = channels.set_agent_mode("101", "off")
		self.assertEqual([c["trigger_id"] for c in result["choices"]], ["1"])

	def test_a_key_without_edit_rights_keeps_the_old_mode(self):
		from dialogbrain.api import channels

		before = self.row("101")
		self.fake.fail["agents.trigger_update"] = DialogBrainError("denied", error_type="permission_denied")
		with self.assertRaises(frappe.ValidationError) as ctx:
			channels.set_agent_mode("101", "off")
		self.assertIn("member, admin or owner", str(ctx.exception))
		self.assertEqual(self.row("101"), before)

	def test_no_bound_trigger_is_refused_without_calling_dialogbrain(self):
		from dialogbrain.api import channels

		self.fake.calls.clear()
		with self.assertRaises(frappe.ValidationError) as ctx:
			channels.set_agent_mode("107", "copilot")
		self.assertIn("This channel has no agent to switch. Open the channel and choose one.", str(ctx.exception))
		self.assertEqual(self.fake.calls, [])

	def test_a_shared_trigger_can_not_be_chosen(self):
		from dialogbrain.api import channels

		before = self.row("105")
		with self.assertRaises(frappe.ValidationError):
			channels.choose_agent("105", "6")
		with self.assertRaises(frappe.ValidationError):
			channels.choose_agent("101", "2")  # another channel's trigger
		self.assertEqual(self.row("105"), before)

	def test_choose_agent_binds_and_reads_its_mode(self):
		from dialogbrain.api import channels

		options = channels.agent_options("104")  # two exclusive agents: not bound
		self.assertEqual(options["mode"], "")
		self.assertEqual(
			sorted((c["trigger_id"], c["agent_name"], c["mode"], c["exclusive"]) for c in options["choices"]),
			[("4", "Support", "copilot", True), ("5", "Sales", "autopilot", True)],
		)
		result = channels.choose_agent("104", "5")
		self.assertEqual(
			(result["trigger_id"], result["agent_id"], result["agent_name"], result["mode"]),
			("5", "10", "Sales", "autopilot"),
		)
		row = self.row("104")
		self.assertEqual((row.agent_trigger_id, row.agent_id, row.agent_mode), ("5", "10", "autopilot"))
		self.assertFalse(row.agent_mode_status)

	def test_agent_options_reads_live_and_refresh_returns_counts(self):
		from dialogbrain.api import channels

		self.fake.agents[10]["triggers"][0]["send_mode"] = "auto"
		self.assertEqual(channels.agent_options("101")["mode"], "autopilot")
		self.assertEqual(self.row("101").agent_mode, "autopilot")
		self.assertEqual(channels.refresh_agent_modes(), {"linked": 3, "ambiguous": 3, "none": 4})

	def test_chat_agent_is_refused(self):
		from dialogbrain.api import channels

		frappe.set_user(self.agent)
		self.fake.calls.clear()
		for call in (
			lambda: channels.agent_options("101"),
			lambda: channels.choose_agent("101", "1"),
			lambda: channels.set_agent_mode("101", "off"),
			lambda: channels.refresh_agent_modes(),
		):
			with self.assertRaises(frappe.PermissionError):
				call()
		self.assertEqual(self.fake.calls, [])
		self.assertEqual(self.row("101").agent_mode, "copilot")

	def test_unknown_mode_is_refused(self):
		from dialogbrain.api import channels

		self.fake.calls.clear()
		with self.assertRaises(frappe.ValidationError):
			channels.set_agent_mode("101", "turbo")
		self.assertEqual(self.fake.calls, [])
		self.assertEqual(self.row("101").agent_mode, "copilot")
