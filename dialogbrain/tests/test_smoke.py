import frappe

from dialogbrain.tests.utils import DialogBrainTestCase


class TestSmoke(DialogBrainTestCase):
	def test_app_installed(self):
		self.assertIn("dialogbrain", frappe.get_installed_apps())
