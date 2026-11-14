"""``DB Webhook Log.lane`` for rows stored before the column existed: a queued backfill
row (``skip_push``) belongs to ``long``, everything else to ``short``. The column has no
default on purpose: a default of ``short`` would have put the queued backfill backlog
ahead of live messages on the first drain after migrating."""

import frappe


def execute():
	frappe.db.sql(
		"""
		update `tabDB Webhook Log`
		set `lane` = case
			when status = 'queued' and json_extract(payload, '$.data.skip_push') = true then 'long'
			else 'short'
		end
		where `lane` is null
		"""
	)
