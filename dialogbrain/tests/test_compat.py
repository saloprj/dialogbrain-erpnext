"""Tests for dialogbrain.compat (Frappe v15 / v16 query differences)."""

from unittest.mock import patch

from dialogbrain import compat
from dialogbrain.tests.utils import DialogBrainTestCase


class TestCompat(DialogBrainTestCase):
	def test_major_version_parses(self):
		for raw, major in (("15.88.1", 15), ("16.56.0", 16), ("17.0.0-dev", 17), ("version-15", 15)):
			with patch.object(compat.frappe, "__version__", raw):
				self.assertEqual(compat.frappe_major(), major)

	def test_aggregate_fields_v16_uses_dict_form(self):
		with patch.object(compat.frappe, "__version__", "16.1.0"):
			self.assertEqual(compat.count_field("cnt"), {"COUNT": "*", "as": "cnt"})
			self.assertEqual(compat.sum_field("grand_total", "total"), {"SUM": "grand_total", "as": "total"})

	def test_aggregate_fields_v15_uses_string_form(self):
		# v15's DatabaseQuery.sanitize_fields calls field.lower(): a dict raises AttributeError
		with patch.object(compat.frappe, "__version__", "15.80.0"):
			self.assertEqual(compat.count_field("cnt"), "count(name) as cnt")
			self.assertEqual(compat.sum_field("grand_total", "total"), "sum(grand_total) as total")
			for f in (compat.count_field("cnt"), compat.sum_field("grand_total", "total")):
				self.assertIsInstance(f, str)
				f.lower()

	def test_sum_field_rejects_unsafe_names(self):
		for bad in ("grand_total) as x, (select 1", "a b"):
			with self.assertRaises(ValueError):
				compat.sum_field(bad, "total")
		with self.assertRaises(ValueError):
			compat.count_field("cnt; drop")

	def test_only_for_on_v15_refuses_with_a_translated_message(self):
		import frappe

		user = "compat-only-for@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc({"doctype": "User", "email": user, "first_name": "Compat"}).insert(ignore_permissions=True)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user(user)
		with patch.object(compat.frappe, "__version__", "15.80.0"):
			with self.assertRaises(frappe.PermissionError) as ctx:
				compat.only_for("System Manager")
			self.assertEqual(str(ctx.exception), "Not permitted")
			with patch.object(compat.frappe, "_", side_effect=lambda text, *a, **k: f"[{text}]"):
				with self.assertRaises(frappe.PermissionError) as ctx:
					compat.only_for(("System Manager",))
			self.assertEqual(str(ctx.exception), "[Not permitted]")
			frappe.set_user("Administrator")
			compat.only_for("System Manager")  # no raise

	def test_single_values_typed_on_both_versions(self):
		# a Check reads as an int (v15 would return the truthy string "0"); a stored Data
		# field keeps its text; a field never stored is absent (reads as None)
		import frappe

		settings = "DialogBrain Settings"
		self.addCleanup(frappe.db.rollback)
		frappe.db.set_single_value(settings, "connected", 0)
		frappe.db.set_single_value(settings, "workspace_id", "ws-1")
		frappe.db.delete("Singles", {"doctype": settings, "field": "dialogbrain_instance"})
		for version in ("15.80.0", "16.1.0"):
			with self.subTest(version=version), patch.object(compat.frappe, "__version__", version):
				values = compat.single_values(settings, ("connected", "workspace_id", "dialogbrain_instance"))
				self.assertIsInstance(values.connected, int)
				self.assertEqual(values.connected, 0)
				self.assertEqual(values.workspace_id, "ws-1")
				self.assertIsNone(values.dialogbrain_instance)
