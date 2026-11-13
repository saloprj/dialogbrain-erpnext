// DialogBrain desk-wide pieces (loaded on every desk page through app_include_js):
//   * a "Chats" item with the unread badge in the sidebar band and the dock,
//   * a short sound on a new inbound message while the inbox is not in focus,
//   * the "Chats" section of the Customer and Lead forms (see public/js/party_chats.js).
// Badge and sound only for Chat Agent / Chat Manager / System Manager.

frappe.provide("frappe.dialogbrain");

(function () {
	const ROLES = ["Chat Agent", "Chat Manager", "System Manager"];
	const INBOX_ROUTE = "dialogbrain-inbox";
	const INBOX_URL = "/app/" + INBOX_ROUTE;
	const POLL_MS = 60 * 1000;
	const EVENT_DEBOUNCE_MS = 2000;
	const SOUND_URL = "/assets/dialogbrain/sounds/new_message.mp3";
	const SOUND_KEY = "dialogbrain:sound"; // "off" mutes; anything else (or nothing) plays
	const SOUND_AT_KEY = "dialogbrain:sound_at"; // last play, shared by the user's tabs
	const SOUND_GAP_MS = 2000;

	const permitted = () =>
		frappe.session && frappe.session.user !== "Guest" && frappe.user.has_role(ROLES);

	function storageGet(key) {
		try {
			return window.localStorage.getItem(key);
		} catch (e) {
			return null; // private mode / blocked storage
		}
	}
	function storageSet(key, value) {
		try {
			window.localStorage.setItem(key, value);
		} catch (e) {
			// not persisted; harmless
		}
	}

	// --- sound -------------------------------------------------------------------------

	frappe.dialogbrain.soundEnabled = () => storageGet(SOUND_KEY) !== "off";
	frappe.dialogbrain.setSoundEnabled = (on) => storageSet(SOUND_KEY, on ? "on" : "off");

	let audio = null;
	function playSound() {
		if (!frappe.dialogbrain.soundEnabled()) return;
		const now = Date.now();
		// several open tabs get the same event: one sound per burst
		const last = parseInt(storageGet(SOUND_AT_KEY) || "0", 10);
		if (now - last < SOUND_GAP_MS) return;
		storageSet(SOUND_AT_KEY, String(now));
		try {
			audio = audio || new Audio(SOUND_URL);
			audio.currentTime = 0;
			const p = audio.play();
			if (p && p.catch) p.catch(() => {}); // autoplay blocked before any user gesture
		} catch (e) {
			// no audio support
		}
	}

	function inboxFocused() {
		const route = frappe.get_route ? frappe.get_route() : [];
		return (
			route &&
			route[0] === INBOX_ROUTE &&
			document.visibilityState === "visible" &&
			document.hasFocus()
		);
	}

	// --- badge -------------------------------------------------------------------------

	let unread = 0;

	function badgeHtml(cls) {
		const label = __("Chats");
		return $(`<a class="dialogbrain-chats-link ${cls}" href="${INBOX_URL}"
				title="${frappe.utils.escape_html(label)}" aria-label="${frappe.utils.escape_html(label)}">
			<span class="dialogbrain-chats-icon">${frappe.utils.icon("message-circle", "sm", "", "", "", true)}</span>
			<span class="dialogbrain-chats-label">${frappe.utils.escape_html(label)}</span>
			<span class="dialogbrain-chats-count hidden" aria-live="polite"></span>
		</a>`);
	}

	// The sidebar and the dock are rebuilt on some navigations: re-attach when missing.
	function ensureBadges() {
		const $band = $(".body-sidebar .standard-items-band").first();
		if ($band.length && !$band.find(".dialogbrain-chats-link").length) {
			$band.append(badgeHtml("dialogbrain-chats-sidebar"));
		}
		const $dock = $(".dock-utilities").first();
		if ($dock.length && !$dock.find(".dialogbrain-chats-link").length) {
			$dock.prepend(badgeHtml("dialogbrain-chats-dock"));
		}
		renderCount();
	}

	function renderCount() {
		const text = unread > 99 ? "99+" : String(unread);
		$(".dialogbrain-chats-count")
			.text(unread > 0 ? text : "")
			.toggleClass("hidden", unread <= 0);
		$(".dialogbrain-chats-link").attr(
			"title",
			unread > 0 ? __("Chats: {0} unread", [unread]) : __("Chats")
		);
	}

	let loading = false;
	async function refreshBadge() {
		if (loading || document.visibilityState !== "visible") return;
		loading = true;
		try {
			const r = await frappe.call({
				method: "dialogbrain.api.conversations.counters",
				type: "GET",
				silent: true,
			});
			unread = (r && r.message && r.message.unread_total) || 0;
			ensureBadges();
		} catch (e) {
			// keep the last value; the next poll retries
		} finally {
			loading = false;
		}
	}

	let eventTimer = null;
	function refreshSoon() {
		clearTimeout(eventTimer);
		eventTimer = setTimeout(refreshBadge, EVENT_DEBOUNCE_MS);
	}

	function init() {
		if (frappe.dialogbrain._deskReady || !permitted()) return;
		frappe.dialogbrain._deskReady = true;

		ensureBadges();
		refreshBadge();
		setInterval(refreshBadge, POLL_MS); // refreshBadge skips while the tab is hidden
		document.addEventListener("visibilitychange", () => {
			if (document.visibilityState === "visible") refreshBadge();
		});
		$(document).on("page-change", () => setTimeout(ensureBadges, 0));

		frappe.realtime.on("dialogbrain:conversation", refreshSoon);
		frappe.realtime.on("dialogbrain:message", (d) => {
			if (!d || d.direction !== "in") return;
			refreshSoon(); // an inbound message raises unread without a conversation event
			// receiving the event means the user is permitted on that conversation
			if (!inboxFocused()) playSound();
		});
	}

	$(document).on("app_ready", init);
	if (frappe.container) setTimeout(init, 0); // the desk started before this file loaded

	// --- Customer / Lead "Chats" section ------------------------------------------------

	const esc = (s) => frappe.utils.escape_html(String(s == null ? "" : s));

	function chatRow(r) {
		const href = `${INBOX_URL}?conversation=${encodeURIComponent(r.name)}`;
		const who = r.title || r.contact_name || r.name;
		const when = r.last_message_at ? frappe.datetime.comment_when(r.last_message_at) : "";
		const unreadBadge = r.unread_count
			? `<span class="indicator-pill red">${esc(r.unread_count)}</span>`
			: "";
		const status = `<span class="indicator-pill ${r.status === "Open" ? "green" : "gray"}">${esc(
			__(r.status)
		)}</span>`;
		return `<a class="dialogbrain-chat-row" href="${esc(href)}">
			<span class="dialogbrain-chat-main">
				<span class="dialogbrain-chat-title">${esc(who)}</span>
				<span class="dialogbrain-chat-meta">${esc(r.channel_title || r.channel_type || "")}</span>
				<span class="dialogbrain-chat-preview">${esc(r.last_message_preview || "")}</span>
			</span>
			<span class="dialogbrain-chat-side">
				${unreadBadge}${status}
				<span class="dialogbrain-chat-when">${when}</span>
			</span>
		</a>`;
	}

	frappe.dialogbrain.renderPartyChats = async function (frm) {
		const parent = frm.dashboard && frm.dashboard.parent;
		if (!parent) return;
		parent.find(".dialogbrain-chats-section").remove();
		if (frm.is_new() || !permitted()) return;

		const docname = frm.doc.name;
		let rows;
		try {
			const r = await frappe.call({
				method: "dialogbrain.api.conversations.list_conversations",
				type: "GET",
				silent: true,
				args: {
					filters: JSON.stringify({ party_type: frm.doctype, party: docname, status: "All" }),
					limit: 20,
				},
			});
			rows = (r && r.message && r.message.rows) || [];
		} catch (e) {
			return; // no access to the inbox: no section
		}
		if (frm.doc.name !== docname) return; // navigated to another record meanwhile
		parent.find(".dialogbrain-chats-section").remove();
		if (!rows.length) return;

		const body = frm.dashboard.add_section(
			`<div class="dialogbrain-chats-list">${rows.map(chatRow).join("")}</div>`,
			__("Chats"),
			"dialogbrain-chats-section"
		);
		body.closest(".form-dashboard-section, .form-section").addClass("dialogbrain-chats-section");
		frm.dashboard.show();
	};
})();
