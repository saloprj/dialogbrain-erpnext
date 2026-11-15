import { createApp } from "vue";
import App from "./App.vue";
import { select, markSelectedReadIfUnread } from "./store.js";

frappe.provide("frappe.dialogbrain");

frappe.dialogbrain.mountInbox = function (el, opts = {}) {
	const app = createApp(App, { opts });
	app.config.globalProperties.__ = window.__;
	app.mount(el);
	return app;
};
frappe.dialogbrain.selectConversation = select;
frappe.dialogbrain.markSelectedReadIfUnread = markSelectedReadIfUnread;
