import frappe
from frappe.tests import IntegrationTestCase


class TestSmoke(IntegrationTestCase):
	def test_app_installed(self):
		self.assertIn("dialogbrain", frappe.get_installed_apps())
