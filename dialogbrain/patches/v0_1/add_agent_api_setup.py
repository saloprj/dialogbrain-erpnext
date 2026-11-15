from dialogbrain.install import create_custom_fields, create_roles


def execute():
	"""Plan 3: the ``DialogBrain Agent API`` role and the ``db_conversation`` custom field
	on Sales Order / Quotation / Lead, for sites installed before Plan 3."""
	create_roles()
	create_custom_fields()
