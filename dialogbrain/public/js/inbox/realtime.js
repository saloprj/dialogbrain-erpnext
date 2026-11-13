// Realtime updates for the inbox page. Server events (dialogbrain/sync/notify.py) go only to
// users permitted on the conversation's channel; each payload carries `conversation`.
//
//   dialogbrain:conversation      -> that list row + counters; the open one: header (+ card
//                                    when its party/contact changed, notes on a notes change)
//   dialogbrain:message / _updated -> open conversation: fetch by name and merge (an
//                                    optimistic row is replaced by client_ref); else the row
//   dialogbrain:message_removed   -> open conversation: drop the row; else the row
//   dialogbrain:draft             -> drafts of the open conversation
//
// Refreshes are debounced at 300 ms per conversation, so a burst of events costs one call.
// Nothing here touches store.composer: unsent text survives every refresh.

import { api } from "./api.js";
import {
	store,
	refreshRow,
	refreshMessages,
	removeMessage,
	loadDrafts,
	loadCounters,
	patchRow,
} from "./store.js";

const DEBOUNCE_MS = 300;
const timers = new Map();

function debounced(key, fn) {
	clearTimeout(timers.get(key));
	timers.set(
		key,
		setTimeout(() => {
			timers.delete(key);
			fn();
		}, DEBOUNCE_MS)
	);
}

// pending work per conversation, flushed by one debounced call
const pending = new Map(); // conversation -> { row, open, notes, messages: Set, markRead }

function queue(conv, what) {
	const p = pending.get(conv) || { row: false, open: false, notes: false, messages: new Set(), markRead: false };
	if (what.row) p.row = true;
	if (what.open) p.open = true;
	if (what.notes) p.notes = true;
	if (what.markRead) p.markRead = true;
	if (what.message) p.messages.add(what.message);
	pending.set(conv, p);
	debounced("conv:" + conv, () => flush(conv));
	if (what.row) debounced("counters", loadCounters);
}

const isOpen = (conv) => !!conv && conv === store.selected;
const pageVisible = () => document.visibilityState === "visible";

async function flush(conv) {
	const p = pending.get(conv);
	pending.delete(conv);
	if (!p) return;
	if (p.messages.size && isOpen(conv)) {
		await refreshMessages(conv, Array.from(p.messages));
	}
	if (p.open && isOpen(conv)) await refreshOpen(conv, p.notes);
	if (p.markRead && isOpen(conv) && pageVisible()) {
		try {
			patchRow(await api.markRead(conv));
		} catch (e) {
			// shown by Frappe
		}
		loadCounters();
	}
	if (p.row) await refreshRow(conv);
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
	queue(d.conversation, { row: true, open: isOpen(d.conversation), notes: !!d.notes });
}

function onMessage(d) {
	if (!d || !d.conversation || !d.message) return;
	const open = isOpen(d.conversation);
	queue(d.conversation, {
		row: true,
		message: open ? d.message : null,
		// an inbound message in the conversation on screen is read
		markRead: open && d.direction === "in",
	});
}

function onMessageUpdated(d) {
	if (!d || !d.conversation || !d.message) return;
	if (isOpen(d.conversation)) queue(d.conversation, { message: d.message });
	else queue(d.conversation, { row: true });
}

function onMessageRemoved(d) {
	if (!d || !d.conversation || !d.message) return;
	removeMessage(d.conversation, d.message);
	queue(d.conversation, { row: true });
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
	return function unsubscribe() {
		for (const [event, fn] of Object.entries(HANDLERS)) frappe.realtime.off(event, fn);
		for (const t of timers.values()) clearTimeout(t);
		timers.clear();
		pending.clear();
	};
}
