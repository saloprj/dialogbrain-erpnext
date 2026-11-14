"""``DB Webhook Log.lane`` for rows stored before the column existed: a backfill row
(``skip_push``) belongs to ``long`` whatever its status (a failed one is retried on its
lane), everything else to ``short``. The column has no default on purpose: a default of
``short`` would have put the queued backfill backlog ahead of live messages on the first
drain after migrating."""

import frappe


def execute():
	frappe.db.sql(
		"""
		update `tabDB Webhook Log`
		set `lane` = case
			when json_extract(payload, '$.data.skip_push') = true then 'long'
			else 'short'
		end
		where `lane` is null
		"""
	)
