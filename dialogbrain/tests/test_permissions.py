import frappe

from dialogbrain.permissions import (
	allowed_channels,
	assert_can_access,
	conversation_query,
	permitted_users,
)
from dialogbrain.party import set_party
from dialogbrain.tests.utils import DialogBrainTestCase

OPEN = "perm-open"
RESTRICTED = "perm-restricted"


def _user(email, *roles, enabled=1):
	if frappe.db.exists("User", email):
		frappe.delete_doc("User", email, force=True, ignore_permissions=True)
	user = frappe.get_doc(
		{"doctype": "User", "email": email, "first_name": email.split("@")[0], "enabled": enabled,
		 "send_welcome_email": 0}
	)
	user.insert(ignore_permissions=True)
	user.add_roles(*roles)
	return email


def _channel(account_id, users=()):
	doc = frappe.get_doc(
		{
			"doctype": "DB Channel",
			"account_id": account_id,
			"channel_type": "whatsapp",
			"allowed_users": [{"user": u} for u in users],
		}
	)
	return doc.insert(ignore_permissions=True).name


def _conv(thread_id, channel):
	return (
		frappe.get_doc({"doctype": "DB Conversation", "thread_id": thread_id, "channel": channel})
		.insert(ignore_permissions=True)
		.name
	)


class TestPermissions(DialogBrainTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete("DB Draft")
		frappe.db.delete("DB Message")
		frappe.db.delete("DB Conversation")
		frappe.db.delete("DB Channel User")
		frappe.db.delete("DB Channel")
		self.agent = _user("perm-agent@example.com", "Chat Agent")
		self.other = _user("perm-other@example.com", "Chat Agent")
		self.manager = _user("perm-manager@example.com", "Chat Manager")
		self.nobody = _user("perm-nobody@example.com")
		self.off = _user("perm-off@example.com", "Chat Agent", enabled=0)
		_channel(OPEN)
		_channel(RESTRICTED, [self.other])
		self.c_open = _conv("perm-t1", OPEN)
		self.c_restricted = _conv("perm-t2", RESTRICTED)
		self.msg = frappe.get_doc(
			{"doctype": "DB Message", "conversation": self.c_restricted, "message_id": "perm-m1",
			 "direction": "in", "text": "hi"}
		).insert(ignore_permissions=True).name

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _names(self, doctype, user):
		frappe.set_user(user)
		return set(frappe.get_list(doctype, pluck="name"))

	def test_agent_sees_open_channel_only(self):
		names = self._names("DB Conversation", self.agent)
		self.assertEqual(names, {self.c_open})
		frappe.set_user(self.agent)
		self.assertTrue(frappe.has_permission("DB Conversation", "read", doc=self.c_open))
		self.assertFalse(frappe.has_permission("DB Conversation", "read", doc=self.c_restricted))
		self.assertFalse(frappe.has_permission("DB Conversation", "write", doc=self.c_restricted))

	def test_allowed_agent_sees_restricted(self):
		self.assertEqual(self._names("DB Conversation", self.other), {self.c_open, self.c_restricted})

	def test_manager_and_admin_see_all(self):
		both = {self.c_open, self.c_restricted}
		self.assertEqual(self._names("DB Conversation", self.manager), both)
		self.assertEqual(self._names("DB Conversation", "Administrator"), both)
		self.assertIsNone(allowed_channels(self.manager))
		self.assertIsNone(allowed_channels("Administrator"))

	def test_no_roles_sees_nothing(self):
		self.assertEqual(allowed_channels(self.nobody), [])
		self.assertEqual(conversation_query(self.nobody), "1=0")
		# No DocPerm at all: Frappe itself refuses the list query.
		with self.assertRaises(frappe.PermissionError):
			self._names("DB Conversation", self.nobody)

	def test_messages_follow_conversation(self):
		self.assertEqual(self._names("DB Message", self.agent), set())
		self.assertEqual(self._names("DB Message", self.other), {self.msg})
		frappe.set_user(self.agent)
		self.assertFalse(frappe.has_permission("DB Message", "read", doc=self.msg))
		frappe.set_user(self.other)
		self.assertTrue(frappe.has_permission("DB Message", "read", doc=self.msg))

	def test_drafts_follow_conversation(self):
		draft = frappe.get_doc(
			{"doctype": "DB Draft", "draft_id": "perm-d1", "conversation": self.c_restricted, "text": "x"}
		).insert(ignore_permissions=True).name
		self.assertEqual(self._names("DB Draft", self.agent), set())
		self.assertEqual(self._names("DB Draft", self.other), {draft})

	def test_permitted_users(self):
		self.assertEqual(
			set(permitted_users(OPEN)) & {self.agent, self.other, self.manager, self.nobody, self.off},
			{self.agent, self.other, self.manager},
		)
		got = set(permitted_users(RESTRICTED))
		self.assertIn(self.other, got)
		self.assertIn(self.manager, got)
		self.assertNotIn(self.agent, got)
		self.assertNotIn(self.nobody, got)

	def test_assert_can_access(self):
		assert_can_access(self.c_open, self.agent)
		with self.assertRaises(frappe.PermissionError):
			assert_can_access(self.c_restricted, self.agent)
		assert_can_access(self.c_restricted, self.manager)

	def test_set_party_forbidden_raises(self):
		frappe.set_user(self.agent)
		with self.assertRaises(frappe.PermissionError):
			set_party(self.c_restricted, "Lead", "nonexistent")

	# --- F5: field-level write permissions ------------------------------------

	def _agent_set(self, field, value, user=None):
		from frappe.client import set_value

		frappe.set_user(user or self.agent)
		try:
			set_value("DB Conversation", self.c_open, field, value)
		except frappe.PermissionError:
			pass  # refused outright is fine too
		finally:
			frappe.set_user("Administrator")
		return frappe.db.get_value("DB Conversation", self.c_open, field)

	def test_agent_cannot_retarget_or_reassign_conversation(self):
		"""F5: a Chat Agent may not move a conversation to another thread/channel (send
		would target it; sync would pull its history into a visible conversation), assign
		it to someone else or relink the party. Status stays theirs to change."""
		frappe.db.set_value("DB Conversation", self.c_open, "thread_ref", "whatsapp:1@s.whatsapp.net")
		for field, value in (
			("thread_id", "perm-t2-hijack"),
			("thread_ref", "telegram:999"),
			("channel", RESTRICTED),
			("assigned_to", self.other),
			("party_type", "Lead"),
			("link_source", "manual"),
			("unread_count", 0),
		):
			before = frappe.db.get_value("DB Conversation", self.c_open, field)
			self.assertEqual(self._agent_set(field, value), before, field)
		self.assertEqual(self._agent_set("status", "Closed"), "Closed")

	def test_manager_can_assign_anyone(self):
		self.assertEqual(self._agent_set("assigned_to", self.other, user=self.manager), self.other)

	def test_agent_reads_protected_fields(self):
		frappe.db.set_value("DB Conversation", self.c_open, "assigned_to", self.other)
		frappe.set_user(self.agent)
		doc = frappe.get_doc("DB Conversation", self.c_open)
		doc.apply_fieldlevel_read_permissions()
		self.assertEqual(doc.thread_id, "perm-t1")
		self.assertEqual(doc.assigned_to, self.other)

	def test_chat_manager_cannot_write_mirror_messages(self):
		"""F5: DB Message rows are written by the server only."""
		from frappe.client import insert

		frappe.set_user(self.manager)
		self.assertFalse(frappe.has_permission("DB Message", "create"))
		self.assertFalse(frappe.has_permission("DB Message", "write", doc=self.msg))
		with self.assertRaises(frappe.PermissionError):
			insert({"doctype": "DB Message", "conversation": self.c_open, "direction": "out", "text": "forged"})


class TestSetPartyRequiresPartyRead(DialogBrainTestCase):
	"""Final wave F1 (R12): a manual link releases the party's orders and invoices to the
	AI agent, so only someone who can read that party in ERPNext may make it."""

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.delete("DB Message")
		frappe.db.delete("DB Conversation")
		frappe.db.delete("DB Channel User")
		frappe.db.delete("DB Channel")
		_channel(OPEN)
		self.conv = _conv("perm-f1", OPEN)
		group = frappe.db.get_value("Customer Group", {"is_group": 0})
		territory = frappe.db.get_value("Territory", {"is_group": 0})
		self.customer = frappe.get_doc(
			{"doctype": "Customer", "customer_name": "Perm F1 Customer", "customer_type": "Individual",
			 "customer_group": group, "territory": territory}
		).insert(ignore_permissions=True).name

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _link(self, user):
		frappe.set_user(user)
		try:
			set_party(self.conv, "Customer", self.customer)
		finally:
			frappe.set_user("Administrator")

	def test_chat_agent_without_customer_read_is_refused(self):
		user = _user("perm-f1-agent@example.com", "Chat Agent")
		with self.assertRaises(frappe.PermissionError) as ctx:
			self._link(user)
		self.assertIn("You don't have access to this customer", str(ctx.exception))
		self.assertFalse(frappe.db.get_value("DB Conversation", self.conv, "party"))

	def test_chat_agent_with_customer_read_links(self):
		user = _user("perm-f1-sales@example.com", "Chat Agent", "Sales User")
		self._link(user)
		d = frappe.db.get_value("DB Conversation", self.conv, ["party", "link_source"], as_dict=1)
		self.assertEqual((d.party, d.link_source), (self.customer, "manual"))

	def test_chat_manager_without_sales_role_is_refused(self):
		user = _user("perm-f1-manager@example.com", "Chat Manager")
		with self.assertRaises(frappe.PermissionError):
			self._link(user)
		self.assertFalse(frappe.db.get_value("DB Conversation", self.conv, "party"))

	def test_create_lead_still_links_the_lead_it_created(self):
		"""The chat's own new Lead has no orders to leak; Chat Agent may still create it."""
		from dialogbrain.party import create_lead

		user = _user("perm-f1-lead@example.com", "Chat Agent")
		frappe.set_user(user)
		try:
			lead = create_lead(self.conv)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value("DB Conversation", self.conv, "party"), lead)
