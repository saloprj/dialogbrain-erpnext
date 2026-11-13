app_name = "dialogbrain"
app_title = "DialogBrain"
app_publisher = "DialogBrain"
app_description = "DialogBrain omnichannel inbox for ERPNext"
app_email = "dev@dialogbrain.com"
app_license = "mit"

required_apps = ["erpnext"]

after_install = "dialogbrain.install.after_install"

permission_query_conditions = {
	"DB Conversation": "dialogbrain.permissions.conversation_query",
	"DB Message": "dialogbrain.permissions.message_query",
	"DB Draft": "dialogbrain.permissions.draft_query",
}

has_permission = {
	"DB Conversation": "dialogbrain.permissions.conversation_has_permission",
	"DB Message": "dialogbrain.permissions.message_has_permission",
	"DB Draft": "dialogbrain.permissions.draft_has_permission",
}
