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
