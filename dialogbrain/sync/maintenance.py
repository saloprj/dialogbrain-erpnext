"""Daily housekeeping."""

from datetime import timedelta

import frappe
from frappe.utils import now_datetime

WEBHOOK_LOG_RETENTION = timedelta(days=14)


def cleanup_webhook_log() -> None:
	"""Delete DB Webhook Log rows received more than 14 days ago (any status)."""
	cutoff = now_datetime() - WEBHOOK_LOG_RETENTION
	frappe.db.sql(
		"""delete from `tabDB Webhook Log`
		where received_at < %(cutoff)s or (received_at is null and creation < %(cutoff)s)""",
		{"cutoff": cutoff},
	)
