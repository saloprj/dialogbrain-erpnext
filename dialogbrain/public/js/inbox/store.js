import { reactive } from "vue";
import { api } from "./api.js";

export const store = reactive({
	selected: null, // conversation name
	conversation: null, // get_conversation() of the selected one
	filters: {
		status: "Open",
		scope: "all", // all | mine | unassigned
		unanswered: false,
		needs_human: false,
		channel_type: null,
		assigned_to: null,
		search: "",
	},
	rows: [],
	nextCursor: null,
	listLoading: false,
	listLoaded: false,
	counters: {},
	channelTypes: [],
	managers: [], // assignable_users() without a conversation; empty for agents
	messages: {}, // conversation -> { rows (oldest first), nextBefore, loading, loaded }
	drafts: {}, // conversation -> pending drafts
	composer: {}, // conversation -> { text, files, clientRef, refSig, draft }
	sending: {}, // conversation -> true while a send / approve is in flight there
	notesVersion: 0, // bumped by a realtime "notes" change of the open conversation
	cardOpen: false,
	cardVersion: 0, // bumped to make the customer card reload
});

// --- list -----------------------------------------------------------------------

let listSeq = 0;

function listFilters() {
	const f = store.filters;
	const out = { status: f.status, scope: f.scope };
	if (f.unanswered) out.unanswered = 1;
	if (f.needs_human) out.needs_human = 1;
	if (f.channel_type) out.channel_type = f.channel_type;
	if (f.assigned_to) out.assigned_to = f.assigned_to;
	if (f.search && f.search.trim()) out.search = f.search.trim();
	return out;
}

export async function loadList(more = false) {
	if (more && (!store.nextCursor || store.listLoading)) return;
	const seq = ++listSeq;
	store.listLoading = true;
	try {
		const r = await api.listConversations(listFilters(), more ? store.nextCursor : null);
		if (seq !== listSeq) return;
		store.rows = more ? store.rows.concat(r.rows) : r.rows;
		store.nextCursor = r.next_cursor;
		store.listLoaded = true;
	} catch (e) {
		// Frappe already showed the error
	} finally {
		if (seq === listSeq) store.listLoading = false;
	}
}

export async function loadCounters() {
	try {
		store.counters = (await api.counters()) || {};
	} catch (e) {
		// shown by Frappe
	}
}

export async function loadListMeta() {
	try {
		const [types, managers] = await Promise.all([api.channelTypes(), api.assignableUsers()]);
		store.channelTypes = types || [];
		store.managers = managers || [];
	} catch (e) {
		// shown by Frappe
	}
}

/** Merge a list row returned by an action (assign, set_status, mark_read). */
export function patchRow(row) {
	if (!row || !row.name) return;
	const i = store.rows.findIndex((r) => r.name === row.name);
	if (i >= 0) store.rows[i] = { ...store.rows[i], ...row };
	if (store.conversation && store.conversation.name === row.name) {
		const { channel, channel_type, channel_title, ...rest } = row;
		Object.assign(store.conversation, rest);
	}
}

/** Row keyset order: COALESCE(last_message_at, creation) desc, name desc (rows carry no
 * creation, so a row without last_message_at keeps its place or goes on top when new). */
function rowAfter(a, b) {
	const ka = String(a.last_message_at || "");
	const kb = String(b.last_message_at || "");
	return ka !== kb ? ka < kb : a.name < b.name;
}

/** Above this many rows in one realtime batch, reload the list instead. */
export const ROW_BATCH_MAX = 50;

/** Realtime: re-read a batch of rows under the list's current filters in ONE call, and put
 * each in place, or drop it when it no longer matches (the open conversation keeps its row).
 * A very large batch reloads the list's first page instead. */
export async function refreshRows(names) {
	names = Array.from(new Set(names || [])).filter(Boolean);
	if (!names.length) return;
	if (names.length > ROW_BATCH_MAX) {
		await loadList();
		return;
	}
	const seq = listSeq;
	let rows;
	try {
		const r = await api.listConversations({ ...listFilters(), name: names }, null, names.length);
		rows = (r && r.rows) || [];
	} catch (e) {
		return;
	}
	if (seq !== listSeq) return; // the list was reloaded meanwhile
	const found = new Map(rows.map((row) => [row.name, row]));
	for (const name of names) placeRow(name, found.get(name) || null);
}

export function refreshRow(name) {
	return refreshRows([name]);
}

function placeRow(name, row) {
	const i = store.rows.findIndex((x) => x.name === name);
	if (!row) {
		if (i >= 0 && name !== store.selected) store.rows.splice(i, 1);
		return;
	}
	if (i >= 0) store.rows.splice(i, 1);
	let j;
	if (!row.last_message_at) {
		j = i >= 0 ? i : 0;
	} else {
		j = store.rows.findIndex((x) => rowAfter(x, row));
		if (j < 0) {
			// sorts after every loaded row: paging brings it, unless it was loaded already
			if (store.nextCursor && i < 0) return;
			j = store.rows.length;
		}
	}
	store.rows.splice(j, 0, row);
}

// --- selection + URL --------------------------------------------------------------

function syncUrl(name) {
	try {
		const url = new URL(window.location.href);
		if (name) url.searchParams.set("conversation", name);
		else url.searchParams.delete("conversation");
		if (url.href !== window.location.href) {
			window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
		}
	} catch (e) {
		// URL sync is a convenience only
	}
}

let convSeq = 0;

export async function select(name, { updateUrl = true } = {}) {
	name = name || null;
	if (updateUrl) syncUrl(name);
	if (store.selected === name && (store.conversation || !name)) return;
	const seq = ++convSeq;
	if (name) composerFor(name);
	store.selected = name;
	store.conversation = null;
	if (!name) {
		store.cardOpen = false;
		return;
	}
	let conv;
	try {
		conv = await api.getConversation(name);
	} catch (e) {
		if (seq === convSeq) {
			store.selected = null;
			syncUrl(null);
		}
		return;
	}
	if (seq !== convSeq) return;
	store.conversation = conv;
	loadMessages(name);
	loadDrafts(name);
	api.markRead(name)
		.then((row) => {
			patchRow(row);
			loadCounters();
		})
		.catch(() => {});
}

export async function refreshConversation() {
	const name = store.selected;
	if (!name) return;
	try {
		const conv = await api.getConversation(name);
		if (store.selected === name) store.conversation = conv;
	} catch (e) {
		// shown by Frappe
	}
}

// --- messages ---------------------------------------------------------------------

function box(conv) {
	if (!store.messages[conv]) {
		store.messages[conv] = { rows: [], nextBefore: null, loading: false, loaded: false };
	}
	return store.messages[conv];
}

export async function loadMessages(conv, earlier = false) {
	const b = box(conv);
	if (b.loading || (earlier && !b.nextBefore)) return;
	b.loading = true;
	try {
		const r = await api.getMessages(conv, earlier ? b.nextBefore : null);
		const rows = (r.rows || []).slice().reverse().filter((m) => !wasRemoved(conv, m.name));
		if (earlier) {
			const known = new Set(b.rows.map((m) => m.name));
			b.rows = rows.filter((m) => !known.has(m.name)).concat(b.rows);
		} else {
			// keep optimistic rows the server does not have yet
			const refs = new Set(rows.map((m) => m.client_ref).filter(Boolean));
			const pending = b.rows.filter((m) => m._optimistic && !refs.has(m.client_ref));
			b.rows = rows.concat(pending);
		}
		b.nextBefore = r.next_before;
		b.loaded = true;
	} catch (e) {
		// shown by Frappe
	} finally {
		b.loading = false;
	}
}

function sortKey(m) {
	return m._optimistic ? "￿" : String(m.sent_at || "");
}

// Names removed by a realtime event, per conversation, for a short while: a fetch that was
// already in flight when the removal arrived must not put the row back.
const REMOVED_TTL_MS = 2 * 60 * 1000;
const removed = new Map(); // conversation -> Map(name -> expiry)

function wasRemoved(conv, name) {
	const m = removed.get(conv);
	if (!m || !m.has(name)) return false;
	if (m.get(name) > Date.now()) return true;
	m.delete(name);
	return false;
}

/** Merge server rows by name, replacing an optimistic row with the same client_ref. */
export function mergeMessages(conv, rows) {
	const b = box(conv);
	let list = b.rows.slice();
	for (const row of rows || []) {
		if (wasRemoved(conv, row.name)) continue;
		let i = list.findIndex((m) => m.name === row.name);
		if (i < 0 && row.client_ref) {
			i = list.findIndex((m) => m._optimistic && m.client_ref === row.client_ref);
		}
		if (i >= 0) list[i] = row;
		else list.push(row);
	}
	// stable sort; optimistic rows stay last
	list = list
		.map((m, i) => [m, i])
		.sort((a, b2) => {
			const ka = sortKey(a[0]);
			const kb = sortKey(b2[0]);
			return ka < kb ? -1 : ka > kb ? 1 : a[1] - b2[1];
		})
		.map((x) => x[0]);
	b.rows = list;
}

export function removeMessage(conv, name, { remember = false } = {}) {
	if (remember) {
		if (!removed.has(conv)) removed.set(conv, new Map());
		removed.get(conv).set(name, Date.now() + REMOVED_TTL_MS);
	}
	const b = store.messages[conv];
	if (b) b.rows = b.rows.filter((m) => m.name !== name);
}

export async function refreshMessages(conv, names) {
	if (!names.length || !store.messages[conv]) return;
	try {
		mergeMessages(conv, await api.getMessagesByName(conv, names, true));
	} catch (e) {
		// a row deleted in between; the next event or reload fixes the view
	}
}

// Fallback while realtime is down: re-check a pending reply a couple of times.
function watchPending(conv, name) {
	[4000, 15000].forEach((ms) =>
		setTimeout(() => {
			const b = store.messages[conv];
			const m = b && b.rows.find((r) => r.name === name);
			if (m && m.delivery_status === "pending") refreshMessages(conv, [name]);
		}, ms)
	);
}

// --- drafts -------------------------------------------------------------------------

export async function loadDrafts(conv) {
	try {
		const rows = await api.getDrafts(conv);
		store.drafts[conv] = rows || [];
	} catch (e) {
		// shown by Frappe
	}
}

function dropDraft(conv, draft) {
	store.drafts[conv] = (store.drafts[conv] || []).filter((d) => d.name !== draft);
	const c = store.composer[conv];
	if (c && c.draft === draft) c.draft = null;
}

const esc = (s) => frappe.utils.escape_html(String(s || ""));

/** approve_draft / reject_draft results: terminal outcomes come back as data, not errors. */
function handleDraftResult(conv, draft, r, approving) {
	if (!r) return;
	if (r.status === "superseded") {
		dropDraft(conv, draft);
		frappe.msgprint({ title: __("Draft"), indicator: "orange", message: esc(r.message) });
	} else if (r.status === "unknown") {
		frappe.msgprint({ title: __("Warning"), indicator: "orange", message: esc(r.message) });
	} else {
		dropDraft(conv, draft);
		if (approving && !r.sent) {
			frappe.msgprint({
				title: __("Warning"),
				indicator: "orange",
				message: esc(r.message || __("The draft was approved but not sent.")),
			});
		} else {
			frappe.show_alert({
				message: approving ? __("Draft approved and sent") : __("Draft rejected"),
				indicator: "green",
			});
		}
	}
}

export async function approveDraft(conv, draft, text = null) {
	try {
		const r = await api.approveDraft(draft, text);
		handleDraftResult(conv, draft, r, true);
		return r;
	} catch (e) {
		return null; // generic error: Frappe showed it (msgprint); the draft stays
	}
}

export async function rejectDraft(conv, draft) {
	try {
		const r = await api.rejectDraft(draft);
		handleDraftResult(conv, draft, r, false);
		return r;
	} catch (e) {
		return null;
	}
}

// --- composer -------------------------------------------------------------------------

export function composerFor(conv) {
	if (!store.composer[conv]) {
		store.composer[conv] = { text: "", files: [], clientRef: null, refSig: null, draft: null };
	}
	return store.composer[conv];
}

export function uuid() {
	if (window.crypto && typeof window.crypto.randomUUID === "function") {
		return window.crypto.randomUUID();
	}
	// insecure context (plain http on a LAN address): RFC 4122 v4 from getRandomValues
	const b = window.crypto.getRandomValues(new Uint8Array(16));
	b[6] = (b[6] & 0x0f) | 0x40;
	b[8] = (b[8] & 0x3f) | 0x80;
	const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
	return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

/**
 * client_ref for this attempt. A failed (or timed-out) send keeps its ref so that sending
 * the SAME text and files again is idempotent: create_reply returns the message the first
 * attempt may already have created. Any change to the text or the files makes a new
 * reply, so it gets a new ref; reusing the old one would return the earlier message and
 * silently drop the edited text.
 */
export function refForAttempt(c, text, fileUrls) {
	const sig = JSON.stringify([text, fileUrls]);
	if (!c.clientRef || c.refSig !== sig) {
		c.clientRef = uuid();
		c.refSig = sig;
	}
	return c.clientRef;
}

export function isSending(conv) {
	return !!store.sending[conv];
}

export async function sendMessage(conv) {
	if (store.sending[conv]) return false;
	store.sending[conv] = true;
	try {
		return await doSend(conv);
	} finally {
		delete store.sending[conv];
	}
}

async function doSend(conv) {
	const c = composerFor(conv);
	const text = c.text.trim();
	if (c.files.some((f) => f.uploading)) return false;
	const files = c.files.filter((f) => f.file_url);

	if (c.draft) {
		if (!text) return false;
		const draft = c.draft;
		const r = await approveDraft(conv, draft, text);
		if (r && r.status !== "unknown") c.text = "";
		return !!r;
	}

	if (!text && !files.length) return false;
	const ref = refForAttempt(c, text, files.map((f) => f.file_url));
	mergeMessages(conv, [
		{
			name: "tmp-" + ref,
			client_ref: ref,
			direction: "out",
			author_type: "manager",
			sent_by: frappe.session.user,
			sent_by_name: frappe.session.user_fullname,
			html: null,
			text,
			sent_at: null,
			delivery_status: "pending",
			attachments: files.map((f) => ({ filename: f.file_name, _local: true })),
			_optimistic: true,
		},
	]);
	c.text = "";
	c.files = [];
	try {
		const r = await api.send(conv, text, ref, files.length ? files.map((f) => f.file_url) : null);
		if (c.clientRef === ref) {
			c.clientRef = null;
			c.refSig = null;
		}
		if (r && r.name) {
			await refreshMessages(conv, [r.name]);
			watchPending(conv, r.name);
		}
		return true;
	} catch (e) {
		// not sent: take the optimistic row back and give the text and files back
		removeMessage(conv, "tmp-" + ref);
		if (!c.text) c.text = text;
		if (!c.files.length) c.files = files;
		return false;
	}
}

export async function retryMessage(conv, name) {
	try {
		await api.retry(name);
	} catch (e) {
		return;
	}
	await refreshMessages(conv, [name]);
	watchPending(conv, name);
}
