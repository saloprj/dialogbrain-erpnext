frappe.pages["dialogbrain-inbox"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Chats"),
		single_column: true,
	});
	page.main.addClass("dialogbrain-inbox-main");
	const el = $('<div class="dialogbrain-inbox-root"></div>').appendTo(page.main)[0];
	wrapper.dialogbrain_inbox = { page, el, mounted: false };

	frappe.require("dialogbrain_inbox.bundle.js").then(() => {
		wrapper.dialogbrain_inbox.app = frappe.dialogbrain.mountInbox(el, {
			page,
			conversation: frappe.utils.get_query_params().conversation || null,
		});
		wrapper.dialogbrain_inbox.mounted = true;
	});
};

frappe.pages["dialogbrain-inbox"].on_page_show = function (wrapper) {
	const state = wrapper.dialogbrain_inbox;
	const conversation = frappe.utils.get_query_params().conversation;
	if (state && state.mounted && conversation && frappe.dialogbrain.selectConversation) {
		frappe.dialogbrain.selectConversation(conversation);
	}
};
