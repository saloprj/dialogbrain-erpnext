"""Clear ``delivery_status`` on inbound DB Messages (fix wave F4).

The Select's first option ("pending") used to be its implicit default, so every inbound
row was stored as ``pending``. Inbound rows have no delivery status (only ``deleted`` is
meaningful for them). Idempotent.
"""

import frappe


def execute():
	frappe.db.sql(
		"""update `tabDB Message` set delivery_status = NULL
		where direction = 'in' and coalesce(delivery_status, '') != 'deleted'"""
	)
