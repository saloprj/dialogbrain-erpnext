const DB_CHANNEL_API = "dialogbrain.api.channels.";

frappe.ui.form.on("DB Channel", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		db_channel_agent.render(frm);
	},
});

const db_channel_agent = {
	render(frm) {
		const group = __("DialogBrain agent");
		const modes = [
			["copilot", __("Copilot")],
			["autopilot", __("Autopilot")],
			["off", __("Off")],
		];
		const manager = frappe.user.has_role("Chat Manager") || frappe.user.has_role("System Manager");
		if (!manager) {
			return;
		}
		if (frm.doc.agent_trigger_id) {
			modes.forEach(([mode, label]) => {
				const button = frm.add_custom_button(label, () => this.set_mode(frm, mode), group);
				if (frm.doc.agent_mode === mode) {
					button.addClass("btn-primary-dark").css("font-weight", "bold");
				}
			});
		}
		frm.add_custom_button(__("Choose agent…"), () => this.choose(frm), group);
		frm.add_custom_button(__("Refresh from DialogBrain"), () => this.refresh_all(frm), group);
		frm.dashboard.clear_headline();
		const mode = frm.doc.agent_mode ? __(frm.doc.agent_mode) : __("none");
		const agent = frm.doc.agent_name ? frappe.utils.escape_html(frm.doc.agent_name) : __("none");
		const status = frm.doc.agent_mode_status ? " - " + frappe.utils.escape_html(frm.doc.agent_mode_status) : "";
		frm.dashboard.set_headline(
			`${__("DialogBrain agent")}: ${agent} · ${__("Mode")}: ${frappe.utils.escape_html(mode)}${status}`
		);
	},

	call(frm, method, args, done) {
		frappe.call({
			method: DB_CHANNEL_API + method,
			type: "POST",
			args,
			freeze: true,
			callback: (r) => done && done(r.message || {}),
		});
	},

	set_mode(frm, mode) {
		this.call(frm, "set_agent_mode", { channel: frm.doc.name, mode }, () => frm.reload_doc());
	},

	refresh_all(frm) {
		this.call(frm, "refresh_agent_modes", {}, () => frm.reload_doc());
	},

	choose(frm) {
		this.call(frm, "agent_options", { channel: frm.doc.name }, (options) => {
			const choices = options.choices || [];
			if (choices.length < 2) {
				frm.reload_doc();
				frappe.show_alert({
					// no choice: the stored status says why (nobody, a shared or catch-all trigger)
					message: choices.length
						? __("Only one agent answers this channel")
						: frappe.utils.escape_html(options.status || __("No agent answers this channel in DialogBrain")),
					indicator: "orange",
				});
				return;
			}
			const dialog = new frappe.ui.Dialog({
				title: __("Choose agent…"),
				fields: [
					{
						fieldname: "trigger_id",
						fieldtype: "Select",
						label: __("DialogBrain agent"),
						reqd: 1,
						options: choices.map((c) => ({ value: c.trigger_id, label: `${c.agent_name} (${__(c.mode)})` })),
						default: options.trigger_id || choices[0].trigger_id,
					},
				],
				primary_action_label: __("Choose"),
				primary_action: (values) => {
					dialog.hide();
					this.call(frm, "choose_agent", { channel: frm.doc.name, trigger_id: values.trigger_id }, () => frm.reload_doc());
				},
			});
			dialog.show();
		});
	},
};
