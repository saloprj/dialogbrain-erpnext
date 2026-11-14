import frappe


def execute():
	"""Plan 4: ``agent_mode`` is read from DialogBrain now. The old default ``copilot``
	claimed a mode nothing backed, so channels without a bound agent trigger lose it.
	Idempotent."""
	frappe.db.sql(
		"""update `tabDB Channel` set agent_mode = ''
		where coalesce(agent_trigger_id, '') = '' and coalesce(agent_mode, '') != ''"""
	)
