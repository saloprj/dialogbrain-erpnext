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
