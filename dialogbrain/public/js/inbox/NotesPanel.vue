<template>
	<div class="db-notes">
		<div class="db-section-title">{{ __("Notes") }}</div>
		<div v-if="notes === null" class="db-muted">{{ __("Loading…") }}</div>
		<div v-else-if="!notes.length" class="db-muted">{{ __("No notes yet") }}</div>
		<div v-for="n in notes || []" :key="n.name" class="db-note">
			<div class="db-note-text">{{ n.text }}</div>
			<div class="db-note-meta">
				<span>{{ n.owner_name || n.owner }} · {{ when(n.creation) }}</span>
				<button
					v-if="n.owner === me || isManager"
					type="button"
					class="btn btn-xs btn-link db-note-del"
					:aria-label="__('Delete note')"
					:title="__('Delete note')"
					@click="remove(n)"
				>
					×
				</button>
			</div>
		</div>
		<textarea
			class="form-control db-note-input"
			rows="2"
			:placeholder="__('Add a note for your team…')"
			v-model="text"
			@keydown.enter.ctrl.prevent="add"
			@keydown.enter.meta.prevent="add"
		></textarea>
		<button type="button" class="btn btn-xs btn-default" :disabled="!text.trim() || saving" @click="add">
			{{ __("Add note") }}
		</button>
	</div>
</template>

<script setup>
import { ref, watch } from "vue";
import { api } from "./api.js";

const props = defineProps({ conversation: { type: String, required: true } });

const me = frappe.session.user;
const isManager = frappe.user.has_role(["Chat Manager", "System Manager"]);
const notes = ref(null);
const text = ref("");
const saving = ref(false);
const drafts = {}; // unsent note text per conversation

const when = (v) => frappe.datetime.comment_when(v);

async function load() {
	const conv = props.conversation;
	notes.value = null;
	try {
		const rows = await api.listNotes(conv);
		if (conv === props.conversation) notes.value = rows || [];
	} catch (e) {
		notes.value = [];
	}
}

watch(
	() => props.conversation,
	(conv, prev) => {
		if (prev) drafts[prev] = text.value;
		text.value = drafts[conv] || "";
		load();
	},
	{ immediate: true }
);

async function add() {
	const value = text.value.trim();
	if (!value || saving.value) return;
	const conv = props.conversation;
	saving.value = true;
	try {
		const note = await api.addNote(conv, value);
		if (conv === props.conversation) {
			text.value = "";
			notes.value = (notes.value || []).concat([{ ...note, owner_name: frappe.session.user_fullname }]);
		}
	} catch (e) {
		// Frappe showed the error
	} finally {
		saving.value = false;
	}
}

function remove(n) {
	frappe.confirm(__("Delete this note?"), async () => {
		try {
			await api.deleteNote(n.name);
			notes.value = (notes.value || []).filter((x) => x.name !== n.name);
		} catch (e) {
			// Frappe showed the error
		}
	});
}

defineExpose({ reload: load });
</script>

<style scoped>
.db-notes {
	display: flex;
	flex-direction: column;
	gap: 6px;
}
.db-section-title {
	font-size: var(--text-xs, 12px);
	font-weight: 600;
	text-transform: uppercase;
	letter-spacing: 0.04em;
	color: var(--text-muted);
}
.db-muted {
	color: var(--text-muted);
	font-size: var(--text-sm, 13px);
}
.db-note {
	padding: 6px 8px;
	border-radius: var(--border-radius, 6px);
	background: var(--bg-yellow);
}
.db-note-text {
	white-space: pre-wrap;
	overflow-wrap: anywhere;
	font-size: var(--text-sm, 13px);
}
.db-note-meta {
	display: flex;
	justify-content: space-between;
	align-items: center;
	font-size: var(--text-xs, 12px);
	color: var(--text-muted);
}
.db-note-del {
	padding: 0 4px;
	color: var(--text-muted);
	text-decoration: none;
	font-size: 14px;
	line-height: 1;
}
.db-note-input {
	resize: vertical;
	min-height: 56px;
	height: 56px;
	font-size: var(--text-sm, 13px);
}
</style>
