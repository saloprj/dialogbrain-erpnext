import frappe

from dialogbrain.party import (
	contact_keys_from_ref,
	create_lead,
	find_parties,
	link_party,
	normalize_phone,
	phones_match,
	set_party,
)
from dialogbrain.tests.utils import DialogBrainTestCase


def _customer(name, phone=None, email=None):
	group = frappe.db.get_value("Customer Group", {"is_group": 0})
	territory = frappe.db.get_value("Territory", {"is_group": 0})
	cust = frappe.get_doc(
		{
			"doctype": "Customer",
			"customer_name": name,
			"customer_type": "Individual",
			"customer_group": group,
			"territory": territory,
		}
	).insert(ignore_permissions=True)
	if phone or email:
		contact = frappe.get_doc(
			{
				"doctype": "Contact",
				"first_name": name,
				"links": [{"link_doctype": "Customer", "link_name": cust.name}],
			}
		)
		if phone:
			contact.append("phone_nos", {"phone": phone, "is_primary_phone": 1})
		if email:
			contact.append("email_ids", {"email_id": email, "is_primary": 1})
		contact.insert(ignore_permissions=True)
	return cust.name


def _channel(account_id, channel_type):
	if frappe.db.exists("DB Channel", account_id):
		return account_id
	return (
		frappe.get_doc({"doctype": "DB Channel", "account_id": account_id, "channel_type": channel_type})
		.insert(ignore_permissions=True)
		.name
	)


def _conv(thread_id, channel_type="whatsapp", **kw):
	ch = _channel(f"acc-{channel_type}", channel_type)
	return (
		frappe.get_doc({"doctype": "DB Conversation", "thread_id": thread_id, "channel": ch, **kw})
		.insert(ignore_permissions=True)
		.name
	)


class TestPartyHelpers(DialogBrainTestCase):
	def test_normalize_phone(self):
		self.assertEqual(normalize_phone("+380 67 123 45 08"), "380671234508")
		self.assertEqual(normalize_phone("00380671234508"), "380671234508")
		self.assertIsNone(normalize_phone("123"))
		self.assertIsNone(normalize_phone(None))

	def test_match_local_vs_international(self):
		self.assertTrue(phones_match("067-123-45-08", "380671234508"))
		self.assertTrue(phones_match("380671234508", "067-123-45-08"))

	def test_no_match_two_countries_same_tail(self):
		self.assertFalse(phones_match("48671234508", "380671234508"))


class TestPartyLinking(DialogBrainTestCase):
	def setUp(self):
		frappe.db.delete("DB Message")
		frappe.db.delete("DB Conversation")

	def tearDown(self):
		frappe.db.rollback()

	def test_autolink_single_customer_verified(self):
		c = _customer("Pty Cust A", phone="+380 67 123 45 08")
		conv = _conv("p-1", "whatsapp", phone="380671234508")
		self.assertEqual(link_party(conv), ("Customer", c))
		d = frappe.db.get_value("DB Conversation", conv, ["party_type", "party", "contact", "link_source"], as_dict=1)
		self.assertEqual((d.party_type, d.party, d.link_source), ("Customer", c, "auto_verified"))
		self.assertTrue(d.contact)

	def test_autolink_livechat_unverified(self):
		_customer("Pty Cust B", phone="+380 67 123 45 08")
		conv = _conv("p-2", "livechat", phone="380671234508")
		self.assertIsNotNone(link_party(conv))
		self.assertEqual(frappe.db.get_value("DB Conversation", conv, "link_source"), "auto_unverified")

	def test_email_match_unverified(self):
		c = _customer("Pty Cust C", email="Pty.C@example.com")
		conv = _conv("p-3", "whatsapp", email="pty.c@EXAMPLE.com")
		self.assertEqual(link_party(conv), ("Customer", c))
		self.assertEqual(frappe.db.get_value("DB Conversation", conv, "link_source"), "auto_unverified")

	def test_no_autolink_when_two_customers(self):
		_customer("Pty Cust D1", phone="380671234508")
		_customer("Pty Cust D2", phone="380671234508")
		conv = _conv("p-4", "whatsapp", phone="380671234508")
		self.assertIsNone(link_party(conv))
		self.assertFalse(frappe.db.get_value("DB Conversation", conv, "party"))

	def test_lead_when_no_customer(self):
		lead = frappe.get_doc(
			{"doctype": "Lead", "first_name": "Pty Lead", "mobile_no": "+380 67 123 45 08"}
		).insert(ignore_permissions=True)
		conv = _conv("p-5", "telegram", phone="380671234508")
		self.assertEqual(link_party(conv), ("Lead", lead.name))
		self.assertEqual(frappe.db.get_value("DB Conversation", conv, "link_source"), "auto_verified")
		self.assertEqual(find_parties("380671234508", None)[0][:2], ("Lead", lead.name))

	def test_manual_link_not_overridden(self):
		a = _customer("Pty Cust E1")
		_customer("Pty Cust E2", phone="380671234508")
		conv = _conv("p-6", "whatsapp", phone="380671234508", party_type="Customer", party=a, link_source="manual")
		self.assertIsNone(link_party(conv))
		d = frappe.db.get_value("DB Conversation", conv, ["party", "link_source"], as_dict=1)
		self.assertEqual((d.party, d.link_source), (a, "manual"))

	def test_set_party_adds_phone_to_contact(self):
		c = _customer("Pty Cust F", phone="+49 151 2345 6789")
		conv = _conv("p-7", "livechat", phone="380671234508", email="f@example.com")
		set_party(conv, "Customer", c)
		d = frappe.db.get_value("DB Conversation", conv, ["party", "contact", "link_source"], as_dict=1)
		self.assertEqual((d.party, d.link_source), (c, "manual"))
		contact = frappe.get_doc("Contact", d.contact)
		self.assertIn("380671234508", [normalize_phone(p.phone) for p in contact.phone_nos])
		self.assertIn("f@example.com", [e.email_id for e in contact.email_ids])
		set_party(conv, "Customer", c)  # idempotent
		contact.reload()
		self.assertEqual(len(contact.phone_nos), 2)

	def test_set_party_creates_contact(self):
		c = _customer("Pty Cust G")
		conv = _conv("p-8", "livechat", phone="380671234508")
		set_party(conv, "Customer", c)
		contact = frappe.get_doc("Contact", frappe.db.get_value("DB Conversation", conv, "contact"))
		self.assertEqual(contact.phone_nos[0].phone, "380671234508")

	def test_create_lead_links_conversation(self):
		conv = _conv("p-9", "livechat", phone="380671234508", contact_name="New Person")
		lead = create_lead(conv)
		d = frappe.db.get_value("DB Conversation", conv, ["party_type", "party", "link_source"], as_dict=1)
		self.assertEqual((d.party_type, d.party, d.link_source), ("Lead", lead, "manual"))
		self.assertEqual(frappe.db.get_value("Lead", lead, "mobile_no"), "380671234508")

	def test_tail_match_on_verified_channel_is_unverified(self):
		c = _customer("Pty Cust H", phone="0671234508")
		conv = _conv("p-10", "whatsapp", phone="+48671234508")
		self.assertEqual(link_party(conv), ("Customer", c))
		self.assertEqual(frappe.db.get_value("DB Conversation", conv, "link_source"), "auto_unverified")

	def test_percent_email_matches_nothing(self):
		frappe.get_doc({"doctype": "Lead", "first_name": "Pty Pct", "email_id": "pct@example.com"}).insert(
			ignore_permissions=True
		)
		self.assertEqual(find_parties(None, "%"), [])
		self.assertEqual(find_parties(None, "pct@exampl_.com"), [])

	def test_set_party_ignores_junk_phone(self):
		c = _customer("Pty Cust I", phone="+49 151 2345 6789")
		conv = _conv("p-11", "livechat", phone="12")
		set_party(conv, "Customer", c)
		contact = frappe.get_doc("Contact", frappe.db.get_value("DB Conversation", conv, "contact"))
		self.assertEqual(len(contact.phone_nos), 1)

	def test_create_lead_twice_creates_one(self):
		conv = _conv("p-12", "livechat", phone="380671234508")
		a = create_lead(conv)
		b = create_lead(conv)
		self.assertEqual(a, b)
		self.assertEqual(frappe.db.count("Lead", {"mobile_no": "380671234508"}), 1)

	def test_set_party_invalid_type(self):
		conv = _conv("p-13", "livechat", phone="380671234508")
		with self.assertRaises(frappe.ValidationError):
			set_party(conv, "Supplier", "x")


class TestContactKeysFromRef(DialogBrainTestCase):
	"""F2: the channel ref is the person's key on WhatsApp / Viber DMs and email DMs."""

	def test_whatsapp_phone_jid(self):
		self.assertEqual(contact_keys_from_ref("whatsapp:996705147979@s.whatsapp.net"), {"phone": "+996705147979"})
		self.assertEqual(contact_keys_from_ref("whatsapp:996705147979:5@s.whatsapp.net"), {"phone": "+996705147979"})
		self.assertEqual(contact_keys_from_ref("whatsapp:996705147979@c.us"), {"phone": "+996705147979"})

	def test_whatsapp_groups_and_lids_have_no_phone(self):
		self.assertEqual(contact_keys_from_ref("whatsapp:120363412225337504@g.us"), {})
		self.assertEqual(contact_keys_from_ref("whatsapp:236399865397379@lid"), {})
		self.assertEqual(contact_keys_from_ref("whatsapp:status@broadcast"), {})
		self.assertEqual(contact_keys_from_ref("whatsapp:12@s.whatsapp.net"), {})  # too short

	def test_viber(self):
		self.assertEqual(contact_keys_from_ref("viber:380501234567"), {"phone": "+380501234567"})
		self.assertEqual(contact_keys_from_ref("viber:id:12345"), {})
		self.assertEqual(contact_keys_from_ref("viber:group:abc"), {})
		self.assertEqual(contact_keys_from_ref("viber:unknown:1"), {})

	def test_email_dm_only(self):
		self.assertEqual(contact_keys_from_ref("email:dm:Ann@Example.com"), {"email": "ann@example.com"})
		self.assertEqual(contact_keys_from_ref("email:group:abc@example.com"), {})
		self.assertEqual(contact_keys_from_ref("email:dm:not-an-address"), {})

	def test_other_channels(self):
		for ref in ("telegram:100000001", "livechat:abc:def", "", None, "linkedin:873:x"):
			self.assertEqual(contact_keys_from_ref(ref), {})
