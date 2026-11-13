try:
	from frappe.tests import IntegrationTestCase as _Base  # Frappe v16
except ImportError:
	from frappe.tests.utils import FrappeTestCase as _Base  # Frappe v15


class DialogBrainTestCase(_Base):
	pass
