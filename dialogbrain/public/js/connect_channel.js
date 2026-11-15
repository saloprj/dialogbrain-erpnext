// "Connect channel": an employee links their own WhatsApp / Telegram number from the desk.
// Talks only to dialogbrain.api.connect_channel.*; the QR is drawn by the server.
// Nothing typed here (phone, code, password) is kept after it is sent.

frappe.provide("frappe.dialogbrain");

(function () {
	const API = "dialogbrain.api.connect_channel.";
	const POLL_MS = 3000;
	const BACKOFF_MS = [6000, 12000];
	// brand names: not translated
	const LABELS = () => ({ whatsapp: "WhatsApp", telegram: "Telegram", viber: "Viber" });
	const METHODS = () => ({
		qr: __("Scan a QR code"),
		pairing_code: __("Enter a code on the phone (no camera)"),
		phone: __("Phone number and login code"),
	});
	const ROLES = ["Chat Agent", "Chat Manager", "System Manager"];

	const call = (method, args) =>
		frappe.call({ method: API + method, type: "POST", args: args || {}, silent: true }).then((r) => r.message);

	// text only: a server message is shown with .text(), never as markup
	const plain = (html) => new DOMParser().parseFromString(String(html), "text/html").body.textContent || "";

	// The server's own sentence (frappe.throw) when there is one, else a general one.
	const errorText = (err) => {
		const body = (err && err.responseJSON) || err || {};
		try {
			const messages = JSON.parse(body._server_messages || "[]");
			const first = messages.length ? JSON.parse(messages[0]) : null;
			if (first && first.message) return plain(first.message);
		} catch (e) {
			// fall through
		}
		if (Array.isArray(err && err.messages) && err.messages.length) return plain(err.messages[0]);
		return __("DialogBrain did not answer. Try again");
	};

	function canConnect() {
		return frappe.session && frappe.session.user !== "Guest" && frappe.user.has_role(ROLES);
	}

	async function open(onConnected) {
		let info;
		try {
			info = await call("options");
		} catch (e) {
			frappe.msgprint(frappe.utils.escape_html(errorText(e)));
			return;
		}
		if (!info || !info.channels || !Object.keys(info.channels).length) {
			frappe.msgprint({
				title: __("Connect channel"),
				message: __("Your DialogBrain server does not support connecting channels from here yet"),
				indicator: "orange",
			});
			return;
		}
		new ConnectDialog(info, onConnected).show();
	}

	class ConnectDialog {
		constructor(info, onConnected) {
			this.info = info;
			this.onConnected = onConnected;
			this.claim = null;
			this.status = null;
			this.waiting = false;
			this.timer = null;
			this.tick = null;
			this.failures = 0;
			this.closed = false;
			this.step = null; // what is drawn now (see render)
			this.seq = 0; // bumped by every submit: an older poll answer is ignored
			this.onVisibility = () => {
				if (!document.hidden && this.claim && this.waiting) this.schedule(0);
			};
		}

		show() {
			const labels = LABELS();
			const channels = Object.keys(this.info.channels);
			const fields = [
				{
					fieldname: "channel",
					fieldtype: "Select",
					label: __("Channel"),
					reqd: 1,
					options: channels.map((c) => ({ value: c, label: labels[c] || frappe.utils.escape_html(c) })),
					default: channels[0],
					onchange: () => this.fillMethods(),
				},
				{ fieldname: "method", fieldtype: "Select", label: __("How to connect"), reqd: 1, options: [] },
			];
			if (this.info.can_choose_owner) {
				fields.push(
					{
						fieldname: "owner_mode",
						fieldtype: "Select",
						label: __("Whose number is it"),
						options: [
							{ value: "me", label: __("Mine") },
							{ value: "employee", label: __("An employee's") },
							{ value: "shared", label: __("Shared (every agent sees it)") },
						],
						default: "me",
					},
					{
						fieldname: "owner",
						fieldtype: "Link",
						options: "User",
						label: __("Employee"),
						depends_on: "eval:doc.owner_mode=='employee'",
						mandatory_depends_on: "eval:doc.owner_mode=='employee'",
						get_query: () => ({ query: "frappe.core.doctype.user.user.user_query", filters: { enabled: 1 } }),
					}
				);
			}
			fields.push({ fieldname: "live", fieldtype: "HTML" });
			this.dialog = new frappe.ui.Dialog({
				title: __("Connect channel"),
				fields,
				primary_action_label: __("Start"),
				primary_action: () => this.start(),
			});
			this.dialog.onhide = () => this.close();
			this.live = this.dialog.fields_dict.live.$wrapper.attr("aria-live", "polite");
			this.fillMethods();
			this.dialog.show();
			document.addEventListener("visibilitychange", this.onVisibility);
		}

		fillMethods() {
			const methods = METHODS();
			const list = this.info.channels[this.dialog.get_value("channel")] || [];
			const field = this.dialog.fields_dict.method;
			field.df.options = list.map((m) => ({ value: m, label: methods[m] || m }));
			field.refresh();
			this.dialog.set_value("method", list[0] || "");
		}

		setup(visible) {
			["channel", "method", "owner_mode", "owner"].forEach((name) => {
				const field = this.dialog.fields_dict[name];
				if (field) field.$wrapper.toggle(visible && (name !== "owner" || this.dialog.get_value("owner_mode") === "employee"));
			});
			this.dialog.get_primary_btn().toggle(visible);
		}

		async start() {
			const values = this.dialog.get_values();
			if (!values) return;
			const args = { channel: values.channel, method: values.method };
			if (values.owner_mode === "employee") args.owner = values.owner;
			if (values.owner_mode === "shared") args.owner = "";
			this.method = values.method;
			this.setup(false);
			this.message(__("Starting…"));
			try {
				const state = await call("start", args);
				if (this.closed) {
					// closed while starting: nobody will poll this connection
					if (state && state.claim) call("cancel", { claim: state.claim }).catch(() => {});
					return;
				}
				this.apply(state);
			} catch (e) {
				this.setup(true);
				this.message(errorText(e), "red");
			}
		}

		apply(state) {
			if (this.closed || !state) return;
			this.claim = state.claim;
			this.status = state.status;
			this.waiting = !!state.waiting;
			this.failures = 0;
			this.render(state);
			if (this.waiting) this.schedule(POLL_MS);
			else this.stop();
			if (state.status === "connected" && this.onConnected) this.onConnected(state);
		}

		schedule(ms) {
			clearTimeout(this.timer);
			if (this.closed || document.hidden) return; // resumes on visibilitychange
			this.timer = setTimeout(() => this.poll(), ms);
		}

		stop() {
			clearTimeout(this.timer);
			clearInterval(this.tick);
		}

		async poll() {
			if (this.closed || !this.claim) return;
			const seq = this.seq;
			try {
				const state = await call("get", { claim: this.claim });
				if (seq !== this.seq) return; // a code was sent meanwhile: its answer is newer
				this.apply(state);
			} catch (e) {
				if (seq !== this.seq) return;
				this.failures += 1;
				const gone = e && (e.status === 404 || e.status === 403);
				if (gone || this.failures > 5) {
					if (!gone) call("cancel", { claim: this.claim }).catch(() => {});
					this.waiting = false;
					this.again(errorText(e));
					return;
				}
				this.schedule(BACKOFF_MS[Math.min(this.failures, BACKOFF_MS.length) - 1]);
			}
		}

		message(text, color) {
			this.step = null;
			this.stopCountdown();
			this.live.empty().append($('<p class="dialogbrain-connect-note"></p>').text(text).css("color", color ? "var(--red-600)" : ""));
		}

		again(text) {
			this.step = null;
			this.stop();
			this.live.empty();
			if (text) this.live.append($('<p class="dialogbrain-connect-note"></p>').text(text));
			$('<button class="btn btn-default btn-sm"></button>')
				.text(__("Start again"))
				.appendTo(this.live)
				.on("click", () => {
					this.claim = null;
					this.setup(true);
					this.live.empty();
				});
		}

		stopCountdown() {
			clearInterval(this.tick);
		}

		countdown(seconds) {
			this.stopCountdown();
			const el = $('<p class="text-muted small dialogbrain-connect-left"></p>').appendTo(this.live);
			let left = Math.max(0, parseInt(seconds, 10) || 0);
			const draw = () => {
				const m = Math.floor(left / 60);
				const s = String(left % 60).padStart(2, "0");
				el.text(__("Time left: {0}", [`${m}:${s}`]));
			};
			draw();
			this.tick = setInterval(() => {
				left = Math.max(0, left - 1);
				draw();
			}, 1000);
		}

		input(kind, label, type) {
			const row = $('<div class="dialogbrain-connect-input"></div>').appendTo(this.live);
			const id = "dialogbrain-connect-" + kind;
			const auto = { password: "new-password", code: "one-time-code", phone: "off" }[kind];
			$('<label class="control-label"></label>').attr("for", id).text(label).appendTo(row);
			const input = $('<input class="form-control">')
				.attr({ id, type: type || "text", autocomplete: auto, inputmode: kind === "code" ? "numeric" : null })
				.appendTo(row);
			const button = $('<button class="btn btn-primary btn-sm"></button>').text(__("Send")).appendTo(row);
			const send = async () => {
				const secret = input.val();
				if (!secret) return;
				input.val("").prop("disabled", true); // never left in the page
				button.prop("disabled", true);
				clearTimeout(this.timer);
				this.seq += 1;
				try {
					const state = await call("submit", { claim: this.claim, kind, secret });
					this.step = null; // the same step again (a wrong code) still gets a fresh input
					this.apply(state);
				} catch (e) {
					input.prop("disabled", false);
					button.prop("disabled", false);
					frappe.show_alert({ message: frappe.utils.escape_html(errorText(e)), indicator: "red" });
					this.schedule(POLL_MS);
				}
			};
			button.on("click", send);
			input.on("keydown", (ev) => ev.key === "Enter" && send());
			setTimeout(() => input.trigger("focus"), 50);
		}

		render(state) {
			const live = this.live;
			const labels = LABELS();
			const name = labels[state.channel_type] || state.channel_type;
			// keep an input the person is typing in: redraw only when the step changes
			const step = [state.status, state.qr_svg || state.qr_image || "", state.pairing_code || ""].join("|");
			if (step === this.step && state.waiting) return;
			this.step = step;
			this.stopCountdown();
			live.empty();
			const note = (text) => $('<p class="dialogbrain-connect-note"></p>').text(text).appendTo(live);

			if (state.status === "connected") {
				note(state.message || __("Connected. New chats of this number will appear in Chats."));
				live.find("p").first().css("color", state.message ? "var(--orange-600)" : "var(--green-600)");
				return;
			}
			if (state.status === "expired") return this.again(__("The time to connect ran out."));
			if (state.status === "error") return this.again(state.error || __("DialogBrain could not connect this number."));

			if (state.status === "need_code") {
				note(__("Enter the login code {0} sent you.", [name]));
				this.input("code", __("Login code"));
			} else if (state.status === "need_password") {
				note(__("This account has two-step verification. Enter its password."));
				this.input("password", __("Password"), "password");
			} else if (state.pairing_code) {
				note(
					state.channel_type === "whatsapp"
						? __("On the phone open WhatsApp → Linked devices → Link a device → Link with phone number, and enter this code:")
						: __("Enter this code in {0} on the phone:", [name])
				);
				$('<div class="dialogbrain-connect-code"></div>').text(state.pairing_code).appendTo(live);
			} else if (state.status === "need_pairing_code" || state.status === "need_phone" || (this.method !== "qr" && !state.qr_svg && !state.qr_image)) {
				note(__("Enter the phone number of the account, with the country code."));
				this.input("phone", __("Phone number"), "tel");
			} else if (state.qr_svg || state.qr_image) {
				const how = {
					whatsapp: __("On the phone open WhatsApp → Linked devices → Link a device, and scan this code."),
					telegram: __("On the phone open Telegram → Settings → Devices → Link Desktop Device, and scan this code."),
				};
				note(
					state.status === "scanned" || state.status === "waiting_confirm"
						? __("Scanned. Confirm on the phone.")
						: how[state.channel_type] || __("Scan this code with {0} on the phone.", [name])
				);
				const box = $('<div class="dialogbrain-connect-qr"></div>').appendTo(live);
				box.attr({ role: "img", "aria-label": __("QR code") });
				if (state.qr_svg) box.html(state.qr_svg); // drawn by our server from the QR text
				else $("<img>").attr("src", state.qr_image).attr("alt", __("QR code")).appendTo(box);
			} else {
				note(__("Waiting for DialogBrain…"));
			}
			this.countdown(state.expires_in);
		}

		close() {
			if (this.closed) return;
			this.closed = true;
			this.stop();
			document.removeEventListener("visibilitychange", this.onVisibility);
			// only a connection that is still waiting is cancelled; a connected one is left alone
			if (this.claim && this.waiting) call("cancel", { claim: this.claim }).catch(() => {});
		}
	}

	frappe.dialogbrain.connect_channel = { open, canConnect };
})();
