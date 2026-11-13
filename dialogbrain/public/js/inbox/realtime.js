// Realtime updates for the inbox page. Server events (dialogbrain/sync/notify.py) go to the
// users permitted on the conversation's channel plus System Managers (Ruling R9); each
// payload carries `conversation`.
//
//   dialogbrain:conversation      -> that list row + counters; the open one: header (+ card
//                                    when its party/contact changed, notes on a notes change)
//   dialogbrain:message / _updated -> the list row; the open conversation also fetches by
//                                    name and merges (an optimistic row is replaced by
//                                    client_ref). An inbound one is marked read only while
//                                    the inbox route is current, visible and focused.
//   dialogbrain:message_removed   -> drop the row (and remember the name for a while so an
//                                    in-flight fetch cannot bring it back); the list row
//   dialogbrain:draft             -> drafts of the open conversation
//
// Debouncing: 300 ms quiet time, but at most 1.5 s from the first event of a burst (maxWait),
// so a steady stream of events still flushes. Open-conversation work is per conversation;
// list rows of ALL conversations are collected into one batch and fetched with ONE
// list_conversations call (store.refreshRows; a very large batch reloads the list instead).
// Nothing here touches store.composer: unsent text survives every refresh.

import { api } from "./api.js";
import {
	store,
	refreshRows,
	refreshMessages,
	removeMessage,
	loadDrafts,
	loadCounters,
	patchRow,
	inboxActive,
	markSelectedReadIfUnread,
} from "./store.js";

const DEBOUNCE_MS = 300;
const MAX_WAIT_MS = 1500;
const timers = new Map(); // key -> { timer, first }

function debounced(key, fn) {
	const now = Date.now();
	const t = timers.get(key) || { timer: null, first: now };
	clearTimeout(t.timer);
	const wait = Math.max(0, Math.min(DEBOUNCE_MS, t.first + MAX_WAIT_MS - now));
	t.timer = setTimeout(() => {
		timers.delete(key);
		fn();
	}, wait);
	timers.set(key, t);
}

const isOpen = (conv) => !!conv && conv === store.selected;

// --- list rows: one batch across conversations --------------------------------------

const rowBatch = new Set();

function queueRow(conv) {
	rowBatch.add(conv);
	debounced("rows", () => {
		const names = Array.from(rowBatch);
		rowBatch.clear();
		refreshRows(names);
		loadCounters();
	});
}

// --- open conversation: per conversation --------------------------------------------

const pending = new Map(); // conversation -> { open, notes, messages: Set, markRead }

function queueOpen(conv, what) {
	const p = pending.get(conv) || { open: false, notes: false, messages: new Set(), markRead: false };
	if (what.open) p.open = true;
	if (what.notes) p.notes = true;
	if (what.markRead) p.markRead = true;
	if (what.message) p.messages.add(what.message);
	pending.set(conv, p);
	debounced("conv:" + conv, () => flush(conv));
}

async function flush(conv) {
	const p = pending.get(conv);
	pending.delete(conv);
	if (!p || !isOpen(conv)) return;
	if (p.messages.size) await refreshMessages(conv, Array.from(p.messages));
	if (p.open && isOpen(conv)) await refreshOpen(conv, p.notes);
	// read only while the manager is actually looking at the inbox (route + visible + focus);
	// otherwise it stays unread and on_page_show / window focus marks it read
	if (p.markRead && isOpen(conv) && inboxActive()) {
		try {
			patchRow(await api.markRead(conv));
		} catch (e) {
			// shown by Frappe
		}
		loadCounters();
	}
}

const CARD_KEYS = ["party_type", "party", "contact", "contact_name", "phone", "email", "handle"];

async function refreshOpen(conv, notes) {
	let fresh;
	try {
		fresh = await api.getConversation(conv);
	} catch (e) {
		return;
	}
	if (!isOpen(conv)) return;
	const before = store.conversation;
	store.conversation = fresh;
	// reload the card only when what it shows changed: a reload re-mounts its sections
	if (!before || CARD_KEYS.some((k) => (before[k] || null) !== (fresh[k] || null))) {
		store.cardVersion++;
	} else if (notes) {
		store.notesVersion++;
	}
}

// --- handlers ----------------------------------------------------------------------

function onConversation(d) {
	if (!d || !d.conversation) return;
	queueRow(d.conversation);
	if (isOpen(d.conversation)) queueOpen(d.conversation, { open: true, notes: !!d.notes });
}

function onMessage(d) {
	if (!d || !d.conversation || !d.message) return;
	queueRow(d.conversation);
	if (isOpen(d.conversation)) {
		// an inbound message in the conversation on screen is read
		queueOpen(d.conversation, { message: d.message, markRead: d.direction === "in" });
	}
}

function onMessageUpdated(d) {
	if (!d || !d.conversation || !d.message) return;
	if (isOpen(d.conversation)) queueOpen(d.conversation, { message: d.message });
	else queueRow(d.conversation);
}

function onMessageRemoved(d) {
	if (!d || !d.conversation || !d.message) return;
	// not fetched by a pending batch (it no longer exists) and never merged back in
	const p = pending.get(d.conversation);
	if (p) p.messages.delete(d.message);
	removeMessage(d.conversation, d.message, { remember: true });
	queueRow(d.conversation);
}

function onDraft(d) {
	if (d && isOpen(d.conversation)) debounced("drafts:" + d.conversation, () => loadDrafts(d.conversation));
}

const HANDLERS = {
	"dialogbrain:conversation": onConversation,
	"dialogbrain:message": onMessage,
	"dialogbrain:message_updated": onMessageUpdated,
	"dialogbrain:message_removed": onMessageRemoved,
	"dialogbrain:draft": onDraft,
};

/** Subscribe; returns the unsubscribe function (call it when the page is destroyed). */
export function subscribe() {
	for (const [event, fn] of Object.entries(HANDLERS)) frappe.realtime.on(event, fn);
	const onFocus = () => markSelectedReadIfUnread();
	const onVisibility = () => document.visibilityState === "visible" && markSelectedReadIfUnread();
	window.addEventListener("focus", onFocus);
	document.addEventListener("visibilitychange", onVisibility);
	return function unsubscribe() {
		for (const [event, fn] of Object.entries(HANDLERS)) frappe.realtime.off(event, fn);
		window.removeEventListener("focus", onFocus);
		document.removeEventListener("visibilitychange", onVisibility);
		for (const t of timers.values()) clearTimeout(t.timer);
		timers.clear();
		pending.clear();
		rowBatch.clear();
	};
}
