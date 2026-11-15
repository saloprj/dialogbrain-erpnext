<template>
	<div class="db-composer">
		<div v-if="c.draft" class="db-editing">
			<span>{{ __("Editing AI draft — Send approves it with your text") }}</span>
			<button type="button" class="btn btn-xs btn-link" @click="cancelDraft">{{ __("Cancel") }}</button>
		</div>
		<div v-if="c.files.length" class="db-files">
			<span v-for="(f, i) in c.files" :key="f.key" class="db-file-chip" :class="{ uploading: f.uploading }">
				<span class="db-file-name">{{ f.uploading ? __("Uploading…") + " " : "" }}{{ f.file_name }}</span>
				<button type="button" class="db-file-x" :aria-label="__('Remove')" :title="__('Remove')" @click="removeFile(i)">×</button>
			</span>
		</div>
		<div v-if="picker.open" class="db-qr" role="listbox" :aria-label="__('Quick replies')">
			<div v-if="replies === null" class="db-qr-empty">{{ __("Loading…") }}</div>
			<div v-else-if="!filtered.length" class="db-qr-empty">{{ __("No quick replies") }}</div>
			<button
				v-for="(r, i) in filtered"
				:key="r.name"
				type="button"
				role="option"
				class="db-qr-item"
				:class="{ active: i === picker.index }"
				:aria-selected="i === picker.index"
				@mousedown.prevent="useReply(r)"
			>
				<span class="db-qr-title">
					<span v-if="r.shortcut" class="db-qr-shortcut">/{{ r.shortcut }}</span>
					{{ r.title }}
				</span>
				<span class="db-qr-text">{{ r.text }}</span>
			</button>
		</div>
		<div class="db-input-row">
			<button
				type="button"
				class="btn btn-default btn-sm db-icon-btn"
				:title="__('Attach files')"
				:aria-label="__('Attach files')"
				:disabled="!!c.draft"
				@click="fileInput.click()"
			>
				📎
			</button>
			<button
				type="button"
				class="btn btn-default btn-sm db-icon-btn"
				:title="__('Quick replies')"
				:aria-label="__('Quick replies')"
				@click="togglePicker"
			>
				⚡
			</button>
			<input ref="fileInput" type="file" multiple class="hidden" @change="onFiles" />
			<textarea
				ref="input"
				class="form-control db-textarea"
				rows="1"
				:placeholder="__('Write a reply…')"
				:title="__('Enter to send, Shift+Enter for a new line, / for quick replies')"
				v-model="c.text"
				@keydown="onKey"
				@input="onInput"
				@blur="picker.open && closePickerSoon()"
			></textarea>
			<button type="button" class="btn btn-primary btn-sm db-send" :disabled="!canSend || sending" @click="send">
				{{ c.draft ? __("Approve") : __("Send") }}
			</button>
		</div>
	</div>
</template>

<script setup>
import { ref, reactive, computed, watch, nextTick, onMounted } from "vue";
import { api } from "./api.js";
import { store, composerFor, sendMessage, isSending } from "./store.js";

const props = defineProps({ conversation: { type: String, required: true } });

const input = ref(null);
const fileInput = ref(null);
// per conversation: a send in flight in A must not disable Send in B
const sending = computed(() => isSending(props.conversation));
const c = computed(() => store.composer[props.conversation] || composerFor(props.conversation));
const canSend = computed(
	() => !c.value.files.some((f) => f.uploading) && (c.value.text.trim() || (!c.value.draft && c.value.files.length))
);

// --- textarea size ---------------------------------------------------------------
function autosize() {
	const el = input.value;
	if (!el) return;
	el.style.height = "auto";
	el.style.height = Math.min(el.scrollHeight + 2, 180) + "px";
}
watch(() => c.value.text, () => nextTick(autosize));
watch(
	() => props.conversation,
	() => {
		picker.open = false;
		replies.value = null;
		nextTick(autosize);
	}
);
onMounted(autosize);

function focus() {
	nextTick(() => {
		const el = input.value;
		if (!el) return;
		el.focus();
		el.setSelectionRange(el.value.length, el.value.length);
	});
}
defineExpose({ focus });

// --- send ------------------------------------------------------------------------
async function send() {
	if (!canSend.value || sending.value) return;
	const conv = props.conversation;
	picker.open = false;
	try {
		await sendMessage(conv);
	} finally {
		if (props.conversation === conv) focus();
	}
}

function cancelDraft() {
	c.value.draft = null;
	c.value.text = "";
}

function onKey(e) {
	if (picker.open) {
		if (e.key === "ArrowDown" || e.key === "ArrowUp") {
			e.preventDefault();
			const n = filtered.value.length;
			if (n) picker.index = (picker.index + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
			return;
		}
		if ((e.key === "Enter" || e.key === "Tab") && filtered.value.length) {
			e.preventDefault();
			useReply(filtered.value[picker.index] || filtered.value[0]);
			return;
		}
		if (e.key === "Escape") {
			e.preventDefault();
			picker.open = false;
			return;
		}
	}
	if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
		e.preventDefault();
		send();
	}
}

// --- quick replies -------------------------------------------------------------------
const replies = ref(null);
const picker = reactive({ open: false, index: 0, slash: false, query: "" });

async function loadReplies() {
	if (replies.value !== null) return;
	const conv = props.conversation;
	try {
		const rows = await api.listQuickReplies(conv);
		if (conv === props.conversation) replies.value = rows || [];
	} catch (e) {
		replies.value = [];
	}
}

const filtered = computed(() => {
	const q = (picker.query || "").toLowerCase();
	const rows = replies.value || [];
	if (!q) return rows.slice(0, 50);
	return rows
		.filter((r) => (r.shortcut || "").toLowerCase().includes(q) || (r.title || "").toLowerCase().includes(q))
		.slice(0, 50);
});
watch(filtered, () => (picker.index = 0));

function openPicker(slash, query = "") {
	Object.assign(picker, { open: true, slash, query, index: 0 });
	loadReplies();
}

function togglePicker() {
	if (picker.open) picker.open = false;
	else {
		openPicker(false);
		focus();
	}
}

// "/" at the start of the text (or after whitespace) opens the picker; the text after it filters.
function slashQuery() {
	const el = input.value;
	if (!el) return null;
	const before = el.value.slice(0, el.selectionStart);
	const m = before.match(/(?:^|\s)\/([^\s/]*)$/);
	return m ? m[1] : null;
}

function onInput() {
	const q = slashQuery();
	if (q !== null) openPicker(true, q);
	else if (picker.slash) picker.open = false;
}

let blurTimer = null;
function closePickerSoon() {
	clearTimeout(blurTimer);
	blurTimer = setTimeout(() => (picker.open = false), 150);
}

function useReply(r) {
	const el = input.value;
	const text = c.value.text;
	if (picker.slash && el) {
		const pos = el.selectionStart;
		const before = text.slice(0, pos).replace(/\/([^\s/]*)$/, "");
		c.value.text = before + r.text + text.slice(pos);
	} else if (el) {
		const pos = el.selectionStart ?? text.length;
		c.value.text = text.slice(0, pos) + r.text + text.slice(pos);
	} else {
		c.value.text = text + r.text;
	}
	picker.open = false;
	focus();
}

// --- files -------------------------------------------------------------------------------
let fileSeq = 0;
function onFiles(e) {
	const files = Array.from(e.target.files || []);
	e.target.value = "";
	const conv = props.conversation;
	for (const file of files) {
		const chip = reactive({ key: ++fileSeq, file_name: file.name, file_url: null, uploading: true });
		const target = composerFor(conv);
		target.files.push(chip);
		api.uploadFile(file)
			.then((doc) => {
				chip.file_url = doc.file_url;
				chip.file_name = doc.file_name || file.name;
				chip.uploading = false;
			})
			.catch((err) => {
				target.files = target.files.filter((f) => f.key !== chip.key);
				frappe.msgprint({
					title: __("Upload failed"),
					indicator: "red",
					message: frappe.utils.escape_html(__("Could not upload {0}: {1}", [file.name, err.message])),
				});
			});
	}
}

function removeFile(i) {
	c.value.files.splice(i, 1);
}
</script>

<style scoped>
.db-composer {
	position: relative;
	border-top: 1px solid var(--border-color);
	padding: 8px 12px;
	background: var(--fg-color);
}
.db-editing {
	display: flex;
	justify-content: space-between;
	align-items: center;
	font-size: var(--text-xs, 12px);
	color: var(--orange-600);
	margin-bottom: 4px;
}
.db-files {
	display: flex;
	flex-wrap: wrap;
	gap: 4px;
	margin-bottom: 6px;
}
.db-file-chip {
	display: inline-flex;
	align-items: center;
	gap: 4px;
	max-width: 100%;
	padding: 1px 4px 1px 8px;
	border-radius: 999px;
	background: var(--control-bg);
	font-size: var(--text-xs, 12px);
}
.db-file-chip.uploading {
	color: var(--text-muted);
}
.db-file-name {
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	max-width: 220px;
}
.db-file-x {
	border: 0;
	background: none;
	color: var(--text-muted);
	cursor: pointer;
	padding: 0 4px;
	font-size: 14px;
	line-height: 1;
}
.db-input-row {
	display: flex;
	gap: 6px;
	align-items: flex-end;
}
.db-icon-btn {
	flex: 0 0 auto;
	padding-left: 8px;
	padding-right: 8px;
}
.db-textarea {
	flex: 1 1 auto;
	min-width: 0;
	resize: none;
	min-height: 32px;
	max-height: 180px;
	line-height: 1.4;
}
.db-send {
	flex: 0 0 auto;
}
.db-qr {
	position: absolute;
	left: 12px;
	right: 12px;
	bottom: calc(100% - 4px);
	max-height: 260px;
	overflow-y: auto;
	background: var(--card-bg, var(--fg-color));
	border: 1px solid var(--border-color);
	border-radius: var(--border-radius-md, 8px);
	box-shadow: var(--shadow-md);
	z-index: 4;
}
.db-qr-empty {
	padding: 10px 12px;
	color: var(--text-muted);
	font-size: var(--text-sm, 13px);
}
.db-qr-item {
	display: flex;
	flex-direction: column;
	width: 100%;
	padding: 6px 12px;
	border: 0;
	background: none;
	color: var(--text-color);
	text-align: left;
	cursor: pointer;
}
.db-qr-item.active,
.db-qr-item:hover {
	background: var(--subtle-fg, var(--control-bg));
}
.db-qr-title {
	font-weight: 500;
	font-size: var(--text-sm, 13px);
}
.db-qr-shortcut {
	color: var(--text-muted);
	margin-right: 4px;
}
.db-qr-text {
	font-size: var(--text-xs, 12px);
	color: var(--text-muted);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
</style>
