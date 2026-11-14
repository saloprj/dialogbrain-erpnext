"""Stamp existing conversations with the current DialogBrain instance (final wave F3, R13).

Only on a connected site: the instance key comes from the current Settings (base URL +
workspace id) and is stored on Settings too. Rows that already carry a key are left alone.
On a disconnected site nothing is stamped: the next Connect stores the key, and a row gets
it when sync next touches it (``upsert_conversation`` fills an empty one).
"""

import frappe

from dialogbrain.instance import SETTINGS, instance_key


def execute():
	settings = frappe.db.get_value(
		SETTINGS, SETTINGS, ["connected", "base_url", "workspace_id", "dialogbrain_instance"], as_dict=True
	)
	if not settings or not frappe.utils.cint(settings.connected):
		return
	key = settings.dialogbrain_instance or instance_key(settings.base_url, settings.workspace_id)
	if not key:
		return
	if not settings.dialogbrain_instance:
		frappe.db.set_single_value(SETTINGS, "dialogbrain_instance", key)
	frappe.db.sql(
		"""update `tabDB Conversation` set dialogbrain_instance = %s
		where dialogbrain_instance is null or dialogbrain_instance = ''""",
		(key,),
	)
