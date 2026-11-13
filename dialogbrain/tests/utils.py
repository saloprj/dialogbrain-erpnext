try:
	from frappe.tests import IntegrationTestCase as _Base  # Frappe v16
except ImportError:
	from frappe.tests.utils import FrappeTestCase as _Base  # Frappe v15


class DialogBrainTestCase(_Base):
	pass


def load_fixture(name):
	"""Load dialogbrain/tests/fixtures/<name>.json (``name`` without extension)."""
	import json
	import os

	path = os.path.join(os.path.dirname(__file__), "fixtures", f"{name}.json")
	with open(path, encoding="utf-8") as f:
		return json.load(f)
