const DB_CONNECT = "dialogbrain.setup.connect";
const DB_PRIVATE_WARNING = __("AI agent can't see ERPNext data — make the site public or use a tunnel");

frappe.ui.form.on("DialogBrain Settings", {
	refresh(frm) {
		dialogbrain_settings.render(frm);
	},

	network_mode(frm) {
		dialogbrain_settings.render_intro(frm);
	},
});

const dialogbrain_settings = {
	render(frm) {
		const connected = !!frm.doc.connected;
		const group = __("DialogBrain");

		frm.add_custom_button(connected ? __("Reconnect") : __("Connect"), () =>
			this.call(frm, "connect", {}, (r) => {
				const res = r.message || {};
				frappe.show_alert({
					message: __("Connected to {0}: {1} channels, history import queued", [
						res.workspace,
						res.channels,
					]),
					indicator: "green",
				});
				this.show_warning(res.warning);
			})
		, connected ? group : null);

		if (connected) {
			frm.add_custom_button(__("Re-sync"), () =>
				this.call(frm, "resync", {}, () =>
					frappe.show_alert({ message: __("History re-import queued"), indicator: "blue" })
				)
			, group);
			if (frm.doc.network_mode !== "private") {
				frm.add_custom_button(__("Check webhook"), () =>
					this.call(frm, "webhook_health", {}, (r) => {
						const h = r.message || {};
						const ok = h.active && !h.failures;
						frappe.show_alert({
							message: ok ? __("Webhook active") : __("Webhook needs attention"),
							indicator: ok ? "green" : "orange",
						});
					})
				, group);
			}
			frm.add_custom_button(__("Disconnect"), () =>
				frappe.confirm(
					__("Stop syncing with DialogBrain? Mirrored conversations stay in ERPNext."),
					() => this.call(frm, "disconnect", {}, () =>
						frappe.show_alert({ message: __("Disconnected"), indicator: "orange" })
					)
				)
			, group);
		}

		this.render_intro(frm);
		this.render_health(frm);
	},

	show_warning(text) {
		if (text) {
			frappe.msgprint({ message: frappe.utils.escape_html(text), indicator: "orange", title: __("Scheduler") });
		}
	},

	call(frm, method, args, after) {
		const run = () =>
			frappe.call({
				method: `${DB_CONNECT}.${method}`,
				args,
				freeze: true,
				freeze_message: __("Talking to DialogBrain…"),
				callback: (r) => {
					after && after(r);
					frm.reload_doc();
				},
			});
		// Connect reads the saved settings (key, mode, site URL): save edits first.
		if (frm.is_dirty()) {
			frm.save().then(run);
		} else {
			run();
		}
	},

	render_intro(frm) {
		frm.set_intro("");
		if (frm.doc.network_mode === "private") {
			// R15: the scheduler tick check (server side, sent with the form)
			const tick_warning = (frm.doc.__onload || {}).scheduler_warning;
			const lines = [DB_PRIVATE_WARNING];
			if (tick_warning) lines.push(frappe.utils.escape_html(tick_warning));
			frm.set_intro(lines.join("<br><br>"), "orange");
		} else if (!frm.doc.connected) {
			frm.set_intro(__("Not connected. Fill in the base URL, API key and public site URL, then press Connect."), "blue");
		}
	},

	render_health(frm) {
		let html = "";
		if (frm.doc.connected && frm.doc.network_mode !== "private") {
			const status = frm.doc.webhook_status || "";
			const bad = !frm.doc.webhook_id || /paused|inactive|fail|no webhook/i.test(status);
			const color = bad ? "var(--red-600)" : "var(--green-600)";
			const text = !frm.doc.webhook_id
				? __("No webhook subscription: press Reconnect")
				: bad
				? __("Webhook needs attention: {0}", [frappe.utils.escape_html(status)])
				: __("Webhook healthy: {0}", [frappe.utils.escape_html(status)]);
			html = `<span style="color:${color}">● ${text}</span>`;
		}
		frm.set_df_property("webhook_status", "description", html);
	},
};
