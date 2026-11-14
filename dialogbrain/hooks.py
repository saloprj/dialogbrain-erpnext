app_name = "dialogbrain"
app_title = "DialogBrain"
app_publisher = "DialogBrain"
app_description = "DialogBrain omnichannel inbox for ERPNext"
app_email = "dev@dialogbrain.com"
app_license = "mit"

required_apps = ["erpnext"]

after_install = "dialogbrain.install.after_install"
before_uninstall = "dialogbrain.install.before_uninstall"

permission_query_conditions = {
	"DB Conversation": "dialogbrain.permissions.conversation_query",
	"DB Message": "dialogbrain.permissions.message_query",
	"DB Draft": "dialogbrain.permissions.draft_query",
	"DB Note": "dialogbrain.permissions.note_query",
	"DB Quick Reply": "dialogbrain.permissions.quick_reply_query",
}

has_permission = {
	"DB Conversation": "dialogbrain.permissions.conversation_has_permission",
	"DB Message": "dialogbrain.permissions.message_has_permission",
	"DB Draft": "dialogbrain.permissions.draft_has_permission",
	"DB Note": "dialogbrain.permissions.note_has_permission",
	"DB Quick Reply": "dialogbrain.permissions.quick_reply_has_permission",
}

scheduler_events = {
	"cron": {"* * * * *": ["dialogbrain.sync.reconcile.tick"]},
	"daily": [
		"dialogbrain.sync.maintenance.cleanup_webhook_log",
		"dialogbrain.sync.maintenance.cleanup_agent_call_log",
	],
}

# Desk-wide: "Chats" unread badge, new-message sound, Customer / Lead "Chats" section.
app_include_js = "dialogbrain_desk.bundle.js"
app_include_css = "dialogbrain_desk.bundle.css"

doctype_js = {
	"Customer": "public/js/party_chats.js",
	"Lead": "public/js/party_chats.js",
}
