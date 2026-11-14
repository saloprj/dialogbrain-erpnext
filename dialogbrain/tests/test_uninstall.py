"""Final wave F4: ``before_uninstall`` leaves nothing of the app's agent access behind."""

from unittest.mock import patch

import frappe
from frappe.utils.password import get_decrypted_password

from dialogbrain.client import DialogBrainError
from dialogbrain.install import DB_CONVERSATION_DOCTYPES, before_uninstall, create_custom_fields
from dialogbrain.tests.test_agent_integration import FakeDialogBrain
from dialogbrain.tests.utils import DialogBrainTestCase

SETTINGS = "DialogBrain Settings"


class TestUninstall(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		from dialogbrain.agent_api import ensure_agent_user
		from frappe.core.doctype.user.user import generate_keys

		create_custom_fields()
		self.user = ensure_agent_user()
		generate_keys(self.user)
		frappe.db.set_single_value(
			SETTINGS,
			{"connected": 1, "integration_id": "77", "network_mode": "public", "base_url": "https://api.example.com",
			 "agent_tool_names": "{}", "allow_agent_writes": 0},
		)
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def tearDown(self):
		frappe.db.rollback()
		create_custom_fields()  # the shared test site keeps its fields
		frappe.set_user("Administrator")

	def _fields(self):
		return frappe.get_all(
			"Custom Field", filters={"fieldname": "db_conversation", "dt": ["in", DB_CONVERSATION_DOCTYPES]},
			fields=["dt", "module"],
		)

	def test_custom_fields_belong_to_the_dialogbrain_module(self):
		rows = self._fields()
		self.assertEqual({r.dt for r in rows}, set(DB_CONVERSATION_DOCTYPES))
		self.assertEqual({r.module for r in rows}, {"DialogBrain"})

	def test_before_uninstall_removes_fields_disables_the_agent_user_and_its_agents(self):
		fake = FakeDialogBrain()
		with patch("dialogbrain.setup.agent_integration._client", return_value=fake):
			before_uninstall()
		self.assertEqual(self._fields(), [])
		user = frappe.db.get_value("User", self.user, ["enabled", "api_key"], as_dict=True)
		self.assertEqual((user.enabled, user.api_key), (0, None))
		self.assertFalse(get_decrypted_password("User", self.user, "api_secret", raise_exception=False))
		switched = fake.called("agents.set_integration")
		self.assertEqual({p["agent_id"] for p in switched}, {5, 6})
		self.assertTrue(all(p["enabled"] is False and p["workspace_integration_id"] == 77 for p in switched))

	def test_before_uninstall_never_fails(self):
		fake = FakeDialogBrain()
		fake.fail["agents.list"] = DialogBrainError("DialogBrain HTTP 503", status=503)
		with patch("dialogbrain.setup.agent_integration._client", return_value=fake), patch("frappe.log_error"):
			before_uninstall()
		with patch("dialogbrain.setup.agent_integration._client", side_effect=frappe.ValidationError("no key")), \
				patch("frappe.log_error"):
			before_uninstall()  # nothing left to do, and no API key: still fine
		self.assertEqual(self._fields(), [])
		self.assertEqual(frappe.db.get_value("User", self.user, "enabled"), 0)

	def test_not_connected_does_not_call_dialogbrain(self):
		frappe.db.set_single_value(SETTINGS, "connected", 0)
		with patch("dialogbrain.setup.agent_integration._client") as client:
			before_uninstall()
		client.assert_not_called()
		self.assertEqual(frappe.db.get_value("User", self.user, "enabled"), 0)
