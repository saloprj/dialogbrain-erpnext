// Thin wrappers around frappe.call for the DialogBrain inbox server API.
const call = (method, args = {}, type = "GET") =>
	frappe
		.call({ method, args, type, freeze: false })
		.then((r) => r.message);

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
	getConversation: (name) => call(CONV + "get_conversation", { name }),
	getMessages: (conversation, before = null, limit = 50) =>
		call(CONV + "get_messages", { conversation, before, limit }),
	getMessagesByName: (conversation, names) =>
		call(CONV + "get_messages_by_name", { conversation, names: JSON.stringify(names) }),
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

	mediaUrl: (message, file_id) =>
		"/api/method/dialogbrain.api.media.get?" +
		new URLSearchParams({ message, file_id }).toString(),
};
