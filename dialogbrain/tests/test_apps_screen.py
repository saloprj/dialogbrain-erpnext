import json
import os

import frappe

from dialogbrain import hooks
from dialogbrain.permissions import INBOX_ROLES, has_app_permission
from dialogbrain.tests.test_permissions import _user
from dialogbrain.tests.utils import DialogBrainTestCase


class TestAppsScreen(DialogBrainTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")

	def test_inbox_roles_match_the_inbox_page(self):
		path = frappe.get_app_path("dialogbrain", "dialogbrain", "page", "dialogbrain_inbox", "dialogbrain_inbox.json")
		with open(path) as f:
			page_roles = {r["role"] for r in json.load(f)["roles"]}
		self.assertEqual(set(INBOX_ROLES), page_roles)

	def test_tile_shown_to_inbox_roles_only(self):
		for role in INBOX_ROLES:
			frappe.set_user(_user(f"apps-{role.replace(' ', '-').lower()}@example.com", role))
			self.assertTrue(has_app_permission(), role)
		frappe.set_user(_user("apps-plain@example.com", "Accounts User"))
		self.assertFalse(has_app_permission())
		frappe.set_user("Administrator")
		self.assertTrue(has_app_permission())

	def test_hook_entry_resolves(self):
		entry = hooks.add_to_apps_screen[0]
		self.assertIs(frappe.get_attr(entry["has_permission"]), has_app_permission)
		self.assertEqual(entry["route"], hooks.app_home)
		prefix = "/desk/" if int(frappe.__version__.split(".")[0]) >= 16 else "/app/"
		self.assertTrue(entry["route"].startswith(prefix), entry["route"])
		logo = entry["logo"].removeprefix("/assets/dialogbrain/")
		self.assertTrue(os.path.exists(frappe.get_app_path("dialogbrain", "public", logo)))
