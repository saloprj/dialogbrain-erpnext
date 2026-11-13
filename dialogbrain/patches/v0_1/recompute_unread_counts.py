"""Recompute ``DB Conversation.unread_count`` (fix wave F1).

Before the fix every inbound row counted, including silently imported history (backfill,
``skip_push`` re-emits) and system events, so counts ran into the thousands.

Rule: count inbound, non-system, not deleted messages that

* came after the conversation's last outgoing message (so 0 when the last message is
  outgoing), and
* were mirrored live, i.e. stored within :data:`LIVE_WINDOW` of being sent. Imported
  history is stored days or years after it was sent; it is not something a manager has
  to read (the same rule the counter follows now: silent imports never count),

and never more than the stored count (a backfill seeded DialogBrain's own, lower count;
Plan 2's "mark read" lowers it too). Idempotent: it only ever lowers a count to the same
value.
"""

import frappe

LIVE_WINDOW_SECONDS = 3600  # webhooks arrive in seconds; reconcile catches up within minutes


def execute():
	if frappe.db.db_type == "postgres":
		live = f"m.creation < m.sent_at + interval '{LIVE_WINDOW_SECONDS} seconds'"
	else:
		live = f"m.creation < date_add(m.sent_at, interval {LIVE_WINDOW_SECONDS} second)"
	frappe.db.sql(
		f"""
		update `tabDB Conversation` c
		set unread_count = least(
			coalesce(c.unread_count, 0),
			(
				select count(*) from `tabDB Message` m
				where m.conversation = c.name
				  and m.direction = 'in'
				  and coalesce(m.author_type, '') != 'system'
				  and coalesce(m.delivery_status, '') != 'deleted'
				  and {live}
				  and m.sent_at > coalesce(
					(select max(o.sent_at) from `tabDB Message` o
					 where o.conversation = c.name and o.direction = 'out'),
					'1900-01-01 00:00:00'
				  )
			)
		)
		"""
	)
