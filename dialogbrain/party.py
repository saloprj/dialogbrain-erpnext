"""Link DialogBrain conversations to ERPNext Customers / Leads by phone or email.

``link_source`` is security-relevant (a later task uses it to decide whether the AI
agent may see the party's data), so it is conservative: only a phone match on a
channel whose phone comes from the channel itself is ``auto_verified``.
"""

import re

import frappe
from frappe import _

# Channel types whose phone is supplied by the channel, not typed by the person.
VERIFIED_PHONE_CHANNELS = {"whatsapp", "viber", "telegram"}

PARTY_TYPES = ("Customer", "Lead")

# WhatsApp JIDs whose user part is the phone number (``@lid`` / ``@g.us`` are not phones).
WHATSAPP_PHONE_SUFFIXES = ("@s.whatsapp.net", "@c.us")
PHONE_DIGITS = (7, 15)  # E.164 has at most 15 digits
EMAIL_RE = re.compile(r"^[^@\s:]+@[^@\s:]+\.[^@\s:]+$")


def normalize_phone(raw):
	digits = re.sub(r"\D", "", raw or "")
	if digits.startswith("00"):
		digits = digits[2:]
	return digits if len(digits) >= 7 else None


def _phone_digits(value):
	digits = (value or "").lstrip("+")
	low, high = PHONE_DIGITS
	return digits if digits.isdigit() and low <= len(digits) <= high else None


def contact_keys_from_ref(ref) -> dict:
	"""Phone / email that a DialogBrain channel ref itself carries (``{}`` if none).

	The one place that knows these ref shapes; used for every conversation upsert
	(webhook, backfill, reconcile), since only webhook messages carry contact metadata.

	* ``whatsapp:<digits>[:<device>]@s.whatsapp.net`` (or ``@c.us``) -> phone ``+<digits>``;
	  ``@lid`` (privacy id), ``@g.us`` (group), ``@broadcast`` -> nothing.
	* ``viber:<digits>`` -> phone; ``viber:id:…`` / ``viber:group:…`` / ``viber:unknown:…``
	  -> nothing.
	* ``email:dm:<address>`` -> email (lower case); ``email:group:…`` -> nothing.

	Whether a phone match is ``auto_verified`` is still decided by :func:`link_party`
	(channel type in VERIFIED_PHONE_CHANNELS and full-number equality, R8/R9).
	"""
	ref = (ref or "").strip()
	channel, _, rest = ref.partition(":")
	channel = channel.lower()
	if channel == "whatsapp":
		if "@" in rest:
			if not rest.lower().endswith(WHATSAPP_PHONE_SUFFIXES):
				return {}
			rest = rest.split("@", 1)[0].split(":", 1)[0]
		digits = _phone_digits(rest)
		return {"phone": f"+{digits}"} if digits else {}
	if channel == "viber":
		digits = _phone_digits(rest)
		return {"phone": f"+{digits}"} if digits else {}
	if channel == "email" and rest.startswith("dm:"):
		address = rest[3:].strip().lower()
		return {"email": address} if EMAIL_RE.match(address) else {}
	return {}


def phones_match(a, b):
	na, nb = normalize_phone(a), normalize_phone(b)
	if not na or not nb:
		return False
	if na == nb:
		return True
	local_a, local_b = na.startswith("0"), nb.startswith("0")
	if local_a == local_b:  # need exactly one local-format side
		return False
	return len(na) >= 9 and len(nb) >= 9 and na[-9:] == nb[-9:]


def _digits_sql(column):
	return f"REGEXP_REPLACE({column}, '[^0-9]', '')"


def _find(phone, email):
	"""Return a list of dicts {party_type, party, matched_by, contact}, Customers first."""
	found = {}  # (party_type, party) -> dict

	def add(party_type, party, matched_by, contact=None, exact=False):
		key = (party_type, party)
		cur = found.get(key)
		if not cur:
			found[key] = {
				"party_type": party_type,
				"party": party,
				"matched_by": matched_by,
				"contact": contact,
				"exact": exact,
			}
			return
		if matched_by == "phone":
			cur["matched_by"] = "phone"
		cur["exact"] = cur["exact"] or exact
		if contact and not cur["contact"]:
			cur["contact"] = contact

	norm = normalize_phone(phone)
	if norm:
		tail = norm[-9:]
		rows = frappe.db.sql(
			f"""SELECT cp.phone, cp.parent AS contact, dl.link_doctype, dl.link_name
			FROM `tabContact Phone` cp
			JOIN `tabDynamic Link` dl ON dl.parent = cp.parent AND dl.parenttype = 'Contact'
			WHERE dl.link_doctype IN ('Customer', 'Lead')
			AND (RIGHT({_digits_sql("cp.phone")}, 9) = %(tail)s)""",
			{"tail": tail},
			as_dict=True,
		)
		for r in rows:
			if phones_match(r.phone, phone):
				add(r.link_doctype, r.link_name, "phone", r.contact, exact=normalize_phone(r.phone) == norm)
		leads = frappe.db.sql(
			f"""SELECT name, mobile_no, phone FROM `tabLead`
			WHERE RIGHT({_digits_sql("mobile_no")}, 9) = %(tail)s
			OR RIGHT({_digits_sql("phone")}, 9) = %(tail)s""",
			{"tail": tail},
			as_dict=True,
		)
		for r in leads:
			if phones_match(r.mobile_no, phone) or phones_match(r.phone, phone):
				add(
					"Lead",
					r.name,
					"phone",
					exact=norm in (normalize_phone(r.mobile_no), normalize_phone(r.phone)),
				)

	email = (email or "").strip().lower()
	if email:
		rows = frappe.db.sql(
			"""SELECT ce.parent AS contact, dl.link_doctype, dl.link_name
			FROM `tabContact Email` ce
			JOIN `tabDynamic Link` dl ON dl.parent = ce.parent AND dl.parenttype = 'Contact'
			WHERE dl.link_doctype IN ('Customer', 'Lead') AND LOWER(ce.email_id) = %s""",
			(email,),
			as_dict=True,
		)
		for r in rows:
			add(r.link_doctype, r.link_name, "email", r.contact)
		for name in frappe.db.sql_list(
			"SELECT name FROM `tabLead` WHERE LOWER(TRIM(email_id)) = %s", (email,)
		):
			add("Lead", name, "email")

	return sorted(found.values(), key=lambda d: d["party_type"] != "Customer")


def find_parties(phone, email):
	return [(d["party_type"], d["party"], d["matched_by"]) for d in _find(phone, email)]


def link_party(conversation):
	conv = frappe.db.get_value(
		"DB Conversation", conversation, ["phone", "email", "channel", "link_source"], as_dict=True
	)
	if not conv or conv.link_source == "manual":
		return None

	matches = _find(conv.phone, conv.email)
	chosen = None
	for ptype in PARTY_TYPES:
		of_type = [m for m in matches if m["party_type"] == ptype]
		if of_type:
			chosen = of_type[0] if len(of_type) == 1 else None
			break
	if not chosen:
		return None

	channel_type = (frappe.db.get_value("DB Channel", conv.channel, "channel_type") or "").lower() if conv.channel else ""
	verified = chosen["matched_by"] == "phone" and chosen["exact"] and channel_type in VERIFIED_PHONE_CHANNELS
	frappe.db.set_value(
		"DB Conversation",
		conversation,
		{
			"party_type": chosen["party_type"],
			"party": chosen["party"],
			"contact": chosen["contact"],
			"link_source": "auto_verified" if verified else "auto_unverified",
		},
	)
	return chosen["party_type"], chosen["party"]


def _check_write(conversation):
	from dialogbrain.permissions import assert_can_access

	assert_can_access(conversation)
	doc = frappe.get_doc("DB Conversation", conversation)
	frappe.has_permission("DB Conversation", "write", doc=doc, throw=True)
	return doc


def _primary_contact(party_type, party):
	if party_type == "Customer":
		primary = frappe.db.get_value("Customer", party, "customer_primary_contact")
		if primary and frappe.db.exists("Contact", primary):
			return primary
	rows = frappe.db.sql(
		"""SELECT dl.parent FROM `tabDynamic Link` dl
		WHERE dl.parenttype = 'Contact' AND dl.link_doctype = %s AND dl.link_name = %s
		ORDER BY dl.creation LIMIT 1""",
		(party_type, party),
	)
	return rows[0][0] if rows else None


def _add_channel_details(contact, phone, email):
	changed = False
	if phone and normalize_phone(phone) and not any(phones_match(p.phone, phone) for p in contact.phone_nos):
		contact.append("phone_nos", {"phone": phone})
		changed = True
	if email and not any((e.email_id or "").lower() == email.lower() for e in contact.email_ids):
		contact.append("email_ids", {"email_id": email})
		changed = True
	return changed


@frappe.whitelist()
def set_party(conversation, party_type, party):
	"""Link the conversation to a party by hand (``link_source = manual``).

	A manual link releases the party's orders and invoices to the AI agent, so the
	caller must be able to read that party in ERPNext (final wave F1, R12): Chat roles
	alone are not enough — the permission is ERPNext's own (e.g. Sales User).
	"""
	conv = _check_write(conversation)
	if party_type not in PARTY_TYPES:
		frappe.throw(_("Party type must be Customer or Lead"))
	if not frappe.db.exists(party_type, party):
		frappe.throw(_("{0} {1} not found").format(party_type, party))
	if not frappe.has_permission(party_type, "read", doc=party):
		label = _("customer") if party_type == "Customer" else _("lead")
		frappe.throw(_("You don't have access to this {0}").format(label), frappe.PermissionError)
	_link(conv, party_type, party)


def _link(conv, party_type, party):
	conversation = conv.name
	contact_name = _primary_contact(party_type, party)
	if contact_name:
		contact = frappe.get_doc("Contact", contact_name)
		if _add_channel_details(contact, conv.phone, conv.email):
			contact.save(ignore_permissions=True)
	elif conv.phone or conv.email:
		contact = frappe.get_doc(
			{
				"doctype": "Contact",
				"first_name": conv.contact_name or frappe.db.get_value(party_type, party, "lead_name" if party_type == "Lead" else "customer_name") or party,
				"links": [{"link_doctype": party_type, "link_name": party}],
			}
		)
		_add_channel_details(contact, conv.phone, conv.email)
		contact.insert(ignore_permissions=True)
		contact_name = contact.name

	frappe.db.set_value(
		"DB Conversation",
		conversation,
		{"party_type": party_type, "party": party, "contact": contact_name, "link_source": "manual"},
	)


@frappe.whitelist()
def create_lead(conversation):
	conv = _check_write(conversation)
	if conv.party_type == "Lead" and conv.party and frappe.db.exists("Lead", conv.party):
		return conv.party
	name = conv.contact_name or conv.phone or conv.email or conv.thread_id
	lead = frappe.get_doc(
		{
			"doctype": "Lead",
			"first_name": name,
			"mobile_no": conv.phone,
			"email_id": conv.email,
			"source": "Chat" if frappe.db.exists("Lead Source", "Chat") else None,
		}
	).insert(ignore_permissions=True)
	# The chat's own new Lead has no orders to release, so no Lead read is required.
	_link(conv, "Lead", lead.name)
	return lead.name
