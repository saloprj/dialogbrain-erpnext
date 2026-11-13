"""Recompute ``DB Conversation.unread_count`` (fix wave F1).

Before the fix every inbound row counted, including silently imported history (backfill,
``skip_push`` re-emits) and system events, so counts ran into the thousands. Rule: the
number of inbound, non-system, not deleted messages after the conversation's last outgoing
message (0 when the last message is outgoing), but never more than the stored count (a
backfill seeded DialogBrain's own, lower count; Plan 2's "mark read" lowers it too).
Idempotent: a second run computes the same value.
"""

import frappe


def execute():
	frappe.db.sql(
		"""
		update `tabDB Conversation` c
		set unread_count = least(
			coalesce(c.unread_count, 0),
			(
				select count(*) from `tabDB Message` m
				where m.conversation = c.name
				  and m.direction = 'in'
				  and coalesce(m.author_type, '') != 'system'
				  and coalesce(m.delivery_status, '') != 'deleted'
				  and m.sent_at > coalesce(
					(select max(o.sent_at) from `tabDB Message` o
					 where o.conversation = c.name and o.direction = 'out'),
					'1900-01-01 00:00:00'
				  )
			)
		)
		"""
	)
