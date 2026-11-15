// Small display helpers shared by the inbox components (all output is plain text).

const CHANNEL_LABELS = {
	telegram: "Telegram",
	telegram_bot: "Telegram Bot",
	whatsapp: "WhatsApp",
	whatsapp_business: "WhatsApp Business",
	livechat: "Live chat",
	email: "Email",
	facebook: "Facebook",
	instagram: "Instagram",
	viber: "Viber",
	linkedin: "LinkedIn",
	discord: "Discord",
	tiktok: "TikTok",
	tiktok_dm: "TikTok DM",
	threads: "Threads",
	x: "X",
	zalo_oa: "Zalo OA",
	avito: "Avito",
	line: "LINE",
};

export function channelLabel(type) {
	if (!type) return "";
	if (CHANNEL_LABELS[type]) return __(CHANNEL_LABELS[type]);
	const words = String(type).replace(/_/g, " ");
	return __(words.charAt(0).toUpperCase() + words.slice(1));
}

export function initials(name) {
	// letters and digits only (no flag/emoji halves), by code point
	const words = String(name || "")
		.split(/\s+/)
		.map((w) => Array.from(w).filter((ch) => /[\p{L}\p{N}]/u.test(ch)))
		.filter((w) => w.length);
	if (!words.length) return "?";
	const letters = words.length > 1 ? words[0][0] + words[1][0] : words[0].slice(0, 2).join("");
	return letters.toUpperCase();
}

/** Server datetimes are in the system time zone; show them in the user's. */
export function userMoment(value) {
	if (!value) return null;
	try {
		return frappe.datetime.convert_to_user_tz(value, false);
	} catch (e) {
		return moment(value);
	}
}

function nowUser() {
	const tz = frappe.boot.time_zone && frappe.boot.time_zone.user;
	return tz && moment.tz ? moment().tz(tz) : moment();
}

/** List time: today HH:mm, otherwise the date. */
export function shortTime(value) {
	const m = userMoment(value);
	if (!m || !m.isValid()) return "";
	if (m.isSame(nowUser(), "day")) return m.format("HH:mm");
	return frappe.datetime.str_to_user(m.format("YYYY-MM-DD"));
}

export function clockTime(value) {
	const m = userMoment(value);
	return m && m.isValid() ? m.format("HH:mm") : "";
}

export function dayLabel(value) {
	const m = userMoment(value);
	if (!m || !m.isValid()) return "";
	const today = nowUser();
	if (m.isSame(today, "day")) return __("Today");
	if (m.isSame(today.clone().subtract(1, "day"), "day")) return __("Yesterday");
	return frappe.datetime.str_to_user(m.format("YYYY-MM-DD"));
}

export function dayKey(value) {
	const m = userMoment(value);
	return m && m.isValid() ? m.format("YYYY-MM-DD") : "";
}

export function formLink(doctype, name) {
	return frappe.utils.get_form_link(doctype, name);
}

export function money(value, currency) {
	return format_currency(value, currency);
}

export function attachmentKind(a) {
	const mime = (a.mime_type || "").toLowerCase();
	const media = (a.media_type || "").toLowerCase();
	if (mime === "application/x-tgsticker") return "file";
	if (mime.startsWith("image/") || ["photo", "image", "sticker"].includes(media)) return "image";
	if (mime.startsWith("video/") || ["video", "video_note", "animation"].includes(media)) return "video";
	if (mime.startsWith("audio/") || ["audio", "voice"].includes(media)) return "audio";
	return "file";
}

export function debounce(fn, ms) {
	let t = null;
	const wrapped = (...args) => {
		clearTimeout(t);
		t = setTimeout(() => fn(...args), ms);
	};
	wrapped.cancel = () => clearTimeout(t);
	return wrapped;
}
