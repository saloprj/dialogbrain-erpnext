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

		const on_connected = (r) => {
			const res = r.message || {};
			this.show_connected(res);
		};

		if (connected) {
			frm.add_custom_button(__("Reconnect"), () => this.call(frm, "connect", {}, on_connected), group);
			frm.add_custom_button(__("Reconnect with DialogBrain"), () => this.oauth_start(frm), group);
		} else {
			frm.add_custom_button(__("Connect with DialogBrain"), () => this.oauth_start(frm));
			frm.change_custom_button_type(__("Connect with DialogBrain"), null, "primary");
			frm.add_custom_button(__("Connect with API key"), () => {
				if (!frm.doc.api_key) {
					frm.fields_dict.api_key_section.collapse(false);
					frm.fields_dict.api_key.$input && frm.fields_dict.api_key.$input.focus();
					return;
				}
				this.call(frm, "connect", {}, on_connected);
			});
		}

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
						frappe.show_alert({
							message: __(
								"Disconnected. You can revoke the key \"ERPNext — {0} MCP\" in DialogBrain → Settings → Developer.",
								[frappe.utils.escape_html(window.location.hostname)]
							),
							indicator: "orange",
						}, 15)
					)
				)
			, group);
		}

		this.render_intro(frm);
		this.render_health(frm);
		this.show_oauth_result(frm);
	},

	oauth_start(frm) {
		// start() reads the saved settings (base URL): save edits first.
		this.save_first(frm, () => this.oauth_start_call());
	},

	save_first(frm, run) {
		if (frm.is_dirty()) {
			frm.save().then(run);
		} else {
			run();
		}
	},

	oauth_start_call() {
		frappe.call({
			method: "dialogbrain.setup.oauth.start",
			freeze: true,
			freeze_message: __("Talking to DialogBrain…"),
			callback: (r) => {
				const url = (r.message || {}).authorize_url;
				if (!url) return;
				if (/^https?:\/\//i.test(url)) {
					window.location.href = url;
				} else {
					frappe.msgprint({
						message: __("DialogBrain returned an invalid sign-in address."),
						indicator: "red",
						title: __("Connect with DialogBrain failed"),
					});
				}
			},
		});
	},

	show_connected(res) {
		frappe.show_alert({
			message: __("Connected to {0}: {1} channels, history import queued", [
				frappe.utils.escape_html(String(res.workspace)),
				frappe.utils.escape_html(String(res.channels)),
			]),
			indicator: "green",
		});
		this.show_warning(res.warning);
	},

	show_oauth_result(frm) {
		const onload = frm.doc.__onload || {};
		const res = onload.oauth_result;
		if (!res) return;
		delete onload.oauth_result; // shown once
		if (res.ok) {
			this.show_connected(res);
			if (res.public_site_url_filled) {
				frappe.msgprint({
					message: __(
						"Webhook address set to {0} — change it if DialogBrain can't reach this address",
						[frappe.utils.escape_html(String(res.public_site_url_filled === true ? frm.doc.public_site_url || "" : res.public_site_url_filled))]
					),
					indicator: "blue",
					title: __("DialogBrain"),
				});
			}
		} else {
			frappe.msgprint({
				message: frappe.utils.escape_html(res.message || ""),
				indicator: "red",
				title: __("Connect with DialogBrain failed"),
			});
		}
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
		this.save_first(frm, run);
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
			frm.set_intro(__("Not connected. Press Connect with DialogBrain, or open Connect with API key and paste a key."), "blue");
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
