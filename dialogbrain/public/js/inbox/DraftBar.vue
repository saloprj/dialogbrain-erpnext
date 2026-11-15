<template>
	<div v-if="drafts.length" class="db-drafts">
		<div v-for="d in drafts" :key="d.name" class="db-draft" :class="{ editing: composer.draft === d.name }">
			<div class="db-draft-head">
				<span class="db-ai">{{ __("AI") }}</span>
				<span class="db-draft-title">{{ __("Draft reply") }}</span>
				<span v-if="composer.draft === d.name" class="db-draft-editing">{{ __("editing below") }}</span>
			</div>
			<div class="db-draft-text">{{ d.text }}</div>
			<div v-if="d.truncated" class="db-draft-hint">
				{{ canFetchFull ? __("Text shortened by DialogBrain — Edit loads the full text") : __("Text shortened by DialogBrain — approve as is or reject") }}
			</div>
			<div class="db-draft-actions">
				<button type="button" class="btn btn-xs btn-primary" :disabled="busy[d.name]" @click="approve(d)">
					{{ __("Approve") }}
				</button>
				<button
					type="button"
					class="btn btn-xs btn-default"
					:disabled="busy[d.name] || (d.truncated && !canFetchFull)"
					:title="d.truncated && !canFetchFull ? __('Text shortened by DialogBrain — approve as is or reject') : ''"
					@click="edit(d)"
				>
					{{ __("Edit") }}
				</button>
				<button type="button" class="btn btn-xs btn-default" :disabled="busy[d.name]" @click="reject(d)">
					{{ __("Reject") }}
				</button>
			</div>
		</div>
	</div>
</template>

<script setup>
import { reactive, computed } from "vue";
import { store, composerFor, approveDraft, rejectDraft, fetchFullDraft } from "./store.js";

const props = defineProps({ conversation: { type: String, required: true } });
const emit = defineEmits(["edit"]);

const drafts = computed(() => store.drafts[props.conversation] || []);
const composer = computed(() => composerFor(props.conversation));
const busy = reactive({});
// The connected DialogBrain can return a draft's full text (agents.get_draft, Plan 3).
const canFetchFull = computed(() => {
	const c = store.conversation;
	return !!(c && c.name === props.conversation && c.permissions && c.permissions.full_drafts);
});

async function run(d, fn) {
	busy[d.name] = true;
	try {
		await fn();
	} finally {
		delete busy[d.name];
	}
}

const approve = (d) => run(d, () => approveDraft(props.conversation, d.name));
const reject = (d) =>
	frappe.confirm(__("Reject this AI draft?"), () => run(d, () => rejectDraft(props.conversation, d.name)));

async function edit(d) {
	if (d.truncated) {
		if (!canFetchFull.value || busy[d.name]) return;
		let full = null;
		await run(d, async () => {
			full = await fetchFullDraft(props.conversation, d.name);
		});
		if (!full || full.truncated) return;
		d = full;
	}
	const c = composer.value;
	if (c.text.trim() && c.draft !== d.name && c.text !== d.text) {
		frappe.confirm(__("Replace the text in the composer with this draft?"), () => startEdit(c, d));
	} else {
		startEdit(c, d);
	}
}

function startEdit(c, d) {
	c.draft = d.name;
	c.text = d.text || "";
	c.files = [];
	emit("edit");
}
</script>

<style scoped>
.db-drafts {
	border-top: 1px solid var(--border-color);
	padding: 8px 12px;
	display: flex;
	flex-direction: column;
	gap: 8px;
	max-height: 40%;
	overflow-y: auto;
	background: var(--fg-color);
}
.db-draft {
	border: 1px solid var(--border-color);
	border-left: 3px solid var(--orange-500);
	border-radius: var(--border-radius, 6px);
	padding: 6px 10px;
	background: var(--bg-orange);
}
.db-draft.editing {
	opacity: 0.75;
}
.db-draft-head {
	display: flex;
	gap: 6px;
	align-items: center;
	font-size: var(--text-xs, 12px);
	color: var(--text-muted);
}
.db-ai {
	font-size: 10px;
	font-weight: 700;
	padding: 0 5px;
	border-radius: 4px;
	background: var(--fg-color);
	color: var(--orange-600);
}
.db-draft-editing {
	font-style: italic;
}
.db-draft-text {
	white-space: pre-wrap;
	overflow-wrap: anywhere;
	margin: 4px 0;
	max-height: 140px;
	overflow-y: auto;
}
.db-draft-hint {
	font-size: var(--text-xs, 12px);
	color: var(--orange-600);
	margin-bottom: 4px;
}
.db-draft-actions {
	display: flex;
	gap: 6px;
	flex-wrap: wrap;
}
</style>
