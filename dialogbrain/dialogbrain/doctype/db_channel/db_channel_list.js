frappe.listview_settings["DB Channel"] = {
	onload(listview) {
		const db = frappe.dialogbrain || {};
		if (!db.connect_channel || !db.connect_channel.canConnect()) return;
		listview.page.add_inner_button(__("Connect channel"), () =>
			db.connect_channel.open(() => listview.refresh())
		);
	},
};
