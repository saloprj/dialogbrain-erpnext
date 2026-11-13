// Thin wrappers around frappe.call for the DialogBrain inbox server API.
// Returns a native Promise; on a server error Frappe has already shown the message, and the
// promise rejects (callers catch to restore UI state, never to show the error twice).
const call = (method, args = {}, type = "GET", extra = {}) =>
	Promise.resolve(frappe.call({ method, args, type, freeze: false, ...extra })).then(
		(r) => r && r.message
	);

const CONV = "dialogbrain.api.conversations.";
const COMP = "dialogbrain.api.composer.";
const CARD = "dialogbrain.api.party_card.";
const INBOX = "dialogbrain.api.inbox.";
const PARTY = "dialogbrain.party.";

export const api = {
	listConversations: (filters, cursor, limit = 50) =>
		call(CONV + "list_conversations", {
			filters: JSON.stringify(filters || {}),
			cursor: cursor ? JSON.stringify(cursor) : null,
			limit,
		}),
	counters: () => call(CONV + "counters"),
	channelTypes: () => call(CONV + "channel_types"),
	assignableUsers: (conversation = null) => call(CONV + "assignable_users", { conversation }),
	getConversation: (name) => call(CONV + "get_conversation", { name }),
	getMessages: (conversation, before = null, limit = 50) =>
		call(CONV + "get_messages", { conversation, before, limit }),
	getMessagesByName: (conversation, names, silent = false) =>
		call(CONV + "get_messages_by_name", { conversation, names: JSON.stringify(names) }, "GET", {
			silent,
		}),
	getDrafts: (conversation) => call(CONV + "get_drafts", { conversation }),
	markRead: (conversation) => call(CONV + "mark_read", { conversation }, "POST"),
	assign: (conversation, user = null) => call(CONV + "assign", { conversation, user }, "POST"),
	setStatus: (conversation, status) => call(CONV + "set_status", { conversation, status }, "POST"),

	listNotes: (conversation) => call(COMP + "list_notes", { conversation }),
	addNote: (conversation, text) => call(COMP + "add_note", { conversation, text }, "POST"),
	deleteNote: (note) => call(COMP + "delete_note", { note }, "POST"),
	listQuickReplies: (conversation) => call(COMP + "list_quick_replies", { conversation }),
	approveDraft: (draft, text = null) => call(COMP + "approve_draft", { draft, text }, "POST"),
	rejectDraft: (draft) => call(COMP + "reject_draft", { draft }, "POST"),

	send: (conversation, text, client_ref, file_urls = null) =>
		call(INBOX + "send", { conversation, text, client_ref, file_urls }, "POST"),
	retry: (message) => call(INBOX + "retry", { message }, "POST"),

	getCard: (conversation) => call(CARD + "get_card", { conversation }),
	searchParties: (conversation, txt) => call(CARD + "search_parties", { conversation, txt }),
	setParty: (conversation, party_type, party) =>
		call(PARTY + "set_party", { conversation, party_type, party }, "POST"),
	createLead: (conversation) => call(PARTY + "create_lead", { conversation }, "POST"),

	// Private upload through Frappe's upload_file; resolves to the File doc ({file_url, file_name}).
	uploadFile: (file) => {
		const form = new FormData();
		form.append("file", file, file.name);
		form.append("is_private", 1);
		form.append("folder", "Home/Attachments");
		return fetch("/api/method/upload_file", {
			method: "POST",
			body: form,
			headers: { Accept: "application/json", "X-Frappe-CSRF-Token": frappe.csrf_token },
		}).then(async (res) => {
			const data = await res.json().catch(() => ({}));
			if (!res.ok || !data.message) {
				let detail = "";
				try {
					detail = JSON.parse(JSON.parse(data._server_messages || "[]")[0] || "{}").message;
				} catch (e) {
					detail = "";
				}
				throw new Error(detail || res.statusText || "upload failed");
			}
			return data.message;
		});
	},

	mediaUrl: (message, file_id) =>
		"/api/method/dialogbrain.api.media.get?" +
		new URLSearchParams({ message, file_id }).toString(),
};
