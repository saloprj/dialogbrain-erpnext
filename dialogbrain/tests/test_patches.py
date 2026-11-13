from datetime import datetime, timedelta

import frappe

from dialogbrain.tests.utils import DialogBrainTestCase

T0 = datetime(2026, 11, 1, 10, 0, 0)


def _channel(account_id="patch-ch", channel_type="telegram"):
	if not frappe.db.exists("DB Channel", account_id):
		frappe.get_doc({"doctype": "DB Channel", "account_id": account_id, "channel_type": channel_type}).insert(
			ignore_permissions=True
		)
	return account_id


def _conv(thread_id, unread, channel="patch-ch", **kw):
	return (
		frappe.get_doc(
			{
				"doctype": "DB Conversation",
				"thread_id": thread_id,
				"channel": _channel(channel),
				"unread_count": unread,
				**kw,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _msg(conversation, db_id, minutes, direction="in", author_type=None, delivery_status=None):
	doc = frappe.get_doc(
		{
			"doctype": "DB Message",
			"conversation": conversation,
			"db_id": db_id,
			"direction": direction,
			"author_type": author_type or ("customer" if direction == "in" else "manager"),
			"sent_at": T0 + timedelta(minutes=minutes),
			"text": db_id,
		}
	).insert(ignore_permissions=True)
	if delivery_status is not None:  # bypass the doctype default, like rows stored before the fix
		frappe.db.set_value("DB Message", doc.name, "delivery_status", delivery_status)
	return doc.name


class TestPatches(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		for dt in ("DB Draft", "DB Message Attachment", "DB Message", "DB Conversation", "DB Channel User", "DB Channel"):
			frappe.db.delete(dt)

	def test_recompute_unread_counts(self):
		"""F1: last message outgoing -> 0; else inbound non-system after the last outgoing,
		never more than the stored count."""
		from dialogbrain.patches.v0_1.recompute_unread_counts import execute

		answered = _conv("p1", 17763)
		_msg(answered, "a1", 1)
		_msg(answered, "a2", 2)
		_msg(answered, "a3", 3, direction="out")

		waiting = _conv("p2", 500)
		_msg(waiting, "b1", 1)
		_msg(waiting, "b2", 2, direction="out")
		_msg(waiting, "b3", 3)
		_msg(waiting, "b4", 4, author_type="system")
		_msg(waiting, "b5", 5)

		never_answered = _conv("p3", 40)
		_msg(never_answered, "c1", 1)
		_msg(never_answered, "c2", 2)

		seeded_lower = _conv("p4", 1)  # backfill seeded DialogBrain's count: keep it
		_msg(seeded_lower, "d1", 1)
		_msg(seeded_lower, "d2", 2)
		_msg(seeded_lower, "d3", 3)

		execute()
		execute()  # idempotent

		get = lambda n: frappe.db.get_value("DB Conversation", n, "unread_count")  # noqa: E731
		self.assertEqual(get(answered), 0)
		self.assertEqual(get(waiting), 2)
		self.assertEqual(get(never_answered), 2)
		self.assertEqual(get(seeded_lower), 1)

	def test_clear_inbound_delivery_status(self):
		"""F4: inbound rows stored with the old Select default "pending" are cleared;
		outgoing rows and deleted inbound rows keep their status."""
		from dialogbrain.patches.v0_1.clear_inbound_delivery_status import execute

		conv = _conv("p5", 0)
		inbound = _msg(conv, "e1", 1, delivery_status="pending")
		deleted = _msg(conv, "e2", 2, delivery_status="deleted")
		pending_out = _msg(conv, "e3", 3, direction="out", delivery_status="pending")
		sent_out = _msg(conv, "e4", 4, direction="out", delivery_status="sent")

		execute()
		execute()  # idempotent

		get = lambda n: frappe.db.get_value("DB Message", n, "delivery_status")  # noqa: E731
		self.assertIsNone(get(inbound))
		self.assertEqual(get(deleted), "deleted")
		self.assertEqual(get(pending_out), "pending")
		self.assertEqual(get(sent_out), "sent")

	def test_fill_contact_keys_from_refs(self):
		"""F2: existing conversations get phone/email from their ref and are linked."""
		from dialogbrain.patches.v0_1.fill_contact_keys_from_refs import execute
		from dialogbrain.tests.test_party import _customer

		digits = "97" + frappe.generate_hash(length=10).translate(str.maketrans("abcdef", "123456"))[:10]
		customer = _customer(f"F2 P {digits}", phone=f"+{digits}")
		wa = _conv("p6", 0, channel="patch-wa", thread_ref=f"whatsapp:{digits}@s.whatsapp.net")
		frappe.db.set_value("DB Channel", "patch-wa", "channel_type", "whatsapp")
		group = _conv("p7", 0, channel="patch-wa", thread_ref="whatsapp:120363412225337504@g.us")
		mail = _conv("p8", 0, channel="patch-mail", thread_ref="email:dm:someone@example.com")
		kept = _conv("p9", 0, channel="patch-mail", thread_ref="email:dm:other@example.com", email="kept@example.com")

		execute()
		execute()  # idempotent

		row = frappe.db.get_value("DB Conversation", wa, ["phone", "party", "link_source"], as_dict=True)
		self.assertEqual((row.phone, row.party, row.link_source), (f"+{digits}", customer, "auto_verified"))
		self.assertIsNone(frappe.db.get_value("DB Conversation", group, "phone"))
		self.assertEqual(frappe.db.get_value("DB Conversation", mail, "email"), "someone@example.com")
		self.assertEqual(frappe.db.get_value("DB Conversation", kept, "email"), "kept@example.com")
