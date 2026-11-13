<template>
	<div class="db-msg" :class="['db-msg-' + side, { 'db-msg-failed': m.delivery_status === 'failed' }]">
		<div class="db-bubble">
			<div v-if="author" class="db-author">
				<span v-if="m.author_type === 'agent'" class="db-ai" :title="__('Written by the AI agent')">{{ __("AI") }}</span>
				<span v-if="authorName">{{ authorName }}</span>
			</div>
			<!-- html is sanitised on the server (sanitize_message_html); never anything else here -->
			<div v-if="m.html" class="db-body" :class="{ 'db-rich': rich }" v-html="m.html"></div>
			<div v-else-if="m.text" class="db-body db-plain">{{ m.text }}</div>
			<AttachmentView v-for="(a, i) in m.attachments || []" :key="a.file_id || i" :message="m.name" :att="a" />
			<div class="db-foot">
				<span v-if="m.delivery_status === 'deleted'" class="db-deleted">{{ __("Deleted") }}</span>
				<span class="db-time" :title="fullTime">{{ clockTime(m.sent_at) }}</span>
				<span v-if="m.direction === 'out'" class="db-status" :class="'db-status-' + status">
					<svg v-if="status === 'pending'" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
						<circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" stroke-width="1.5" />
						<path d="M8 4.5V8l2.5 1.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
					</svg>
					<svg v-else-if="status === 'sent'" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
						<path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
					</svg>
					<span v-else-if="status === 'unknown'" class="db-unknown" :title="__('Delivery status unknown: DialogBrain did not confirm this message. Check the conversation before sending it again.')">?</span>
					<span v-else-if="m.delivery_status === 'failed'" class="db-failed-mark">!</span>
					<span class="sr-only">{{ statusLabel }}</span>
				</span>
			</div>
			<div v-if="m.delivery_status === 'failed' || (status === 'unknown' && m.can_retry)" class="db-failed">
				<span class="db-error">{{ m.delivery_status === "failed" ? m.error || __("Not delivered") : __("Delivery not confirmed") }}</span>
				<button type="button" class="btn btn-xs btn-default" :disabled="retrying" @click="retry">
					{{ retrying ? __("Retrying…") : __("Retry") }}
				</button>
			</div>
		</div>
	</div>
</template>

<script setup>
import { ref, computed } from "vue";
import AttachmentView from "./AttachmentView.vue";
import { retryMessage } from "./store.js";
import { clockTime, userMoment } from "./utils.js";

const props = defineProps({
	m: { type: Object, required: true },
	conversation: { type: String, required: true },
});

const side = computed(() => {
	if (props.m.author_type === "system") return "system";
	return props.m.direction === "out" ? "out" : "in";
});
const authorName = computed(() => (props.m.author_type === "manager" ? props.m.sent_by_name || props.m.sent_by : null));
const author = computed(() => props.m.author_type === "agent" || !!authorName.value);
// Plain text (e.g. manager replies) keeps its line breaks; real HTML lays itself out.
const rich = computed(() => /<(p|div|br|ul|ol|li|table|h[1-6]|blockquote|pre)\b/i.test(props.m.html || ""));
const fullTime = computed(() => {
	const t = userMoment(props.m.sent_at);
	return t && t.isValid() ? frappe.datetime.str_to_user(t.format("YYYY-MM-DD HH:mm:ss")) : "";
});
// A claimed reply (unknown, no error, just now) is still in flight: show it as sending.
const status = computed(() => (props.m.in_flight ? "pending" : props.m.delivery_status || "sent"));
const statusLabel = computed(
	() =>
		({
			pending: __("Sending"),
			sent: __("Sent"),
			unknown: __("Delivery status unknown"),
			failed: __("Failed"),
			deleted: __("Deleted"),
		})[status.value] || ""
);

const retrying = ref(false);
function retry() {
	if (props.m.delivery_status !== "unknown") return doRetry();
	// Plan 3: sent again with the same request_id, so DialogBrain skips a send it already made
	frappe.confirm(
		__("DialogBrain did not confirm this message. Send it again? If it was already delivered, DialogBrain recognises the repeat."),
		doRetry
	);
}

async function doRetry() {
	retrying.value = true;
	try {
		await retryMessage(props.conversation, props.m.name);
	} finally {
		retrying.value = false;
	}
}
</script>

<style scoped>
.db-msg {
	display: flex;
	margin: 3px 0;
}
.db-msg-in {
	justify-content: flex-start;
}
.db-msg-out {
	justify-content: flex-end;
}
.db-msg-system {
	justify-content: center;
}
.db-bubble {
	max-width: min(78%, 640px);
	min-width: 64px;
	padding: 6px 10px 4px;
	border-radius: 12px;
	background: var(--control-bg);
	color: var(--text-color);
	word-wrap: break-word;
	overflow-wrap: anywhere;
}
.db-msg-in .db-bubble {
	border-bottom-left-radius: 4px;
	background: var(--control-bg);
}
.db-msg-out .db-bubble {
	border-bottom-right-radius: 4px;
	background: var(--bg-blue);
}
.db-msg-system .db-bubble {
	background: transparent;
	border: 1px dashed var(--border-color);
	color: var(--text-muted);
	font-size: var(--text-xs, 12px);
	max-width: 90%;
	text-align: center;
}
.db-msg-failed .db-bubble {
	background: var(--bg-red);
}
.db-author {
	display: flex;
	gap: 6px;
	align-items: center;
	font-size: var(--text-xs, 12px);
	color: var(--text-muted);
	margin-bottom: 2px;
}
.db-ai {
	font-size: 10px;
	font-weight: 700;
	letter-spacing: 0.04em;
	padding: 0 5px;
	border-radius: 4px;
	background: var(--bg-orange);
	color: var(--orange-600);
}
/* Server-sanitised HTML stays inside the bubble: no overflow, no painting outside. */
.db-body {
	overflow: hidden;
	contain: paint;
	max-width: 100%;
}
.db-plain,
.db-body:not(.db-rich) {
	white-space: pre-wrap;
}
.db-body :deep(img) {
	max-width: 100%;
	height: auto;
}
.db-body :deep(p) {
	margin: 0 0 4px;
}
.db-body :deep(p:last-child) {
	margin-bottom: 0;
}
.db-body :deep(a) {
	text-decoration: underline;
}
.db-body :deep(table) {
	max-width: 100%;
	display: block;
	overflow-x: auto;
}
.db-foot {
	display: flex;
	justify-content: flex-end;
	align-items: center;
	gap: 4px;
	font-size: 10px;
	color: var(--text-muted);
	margin-top: 2px;
}
.db-msg-system .db-foot {
	justify-content: center;
}
.db-deleted {
	font-style: italic;
}
.db-status {
	display: inline-flex;
	align-items: center;
}
.db-status-sent {
	color: var(--green-600);
}
.db-unknown {
	display: inline-block;
	width: 12px;
	height: 12px;
	line-height: 12px;
	text-align: center;
	border-radius: 50%;
	border: 1px solid currentColor;
	font-size: 9px;
	font-weight: 700;
	color: var(--orange-600);
	cursor: help;
}
.db-failed-mark {
	color: var(--red-600);
	font-weight: 700;
}
.db-failed {
	display: flex;
	gap: 8px;
	align-items: center;
	justify-content: space-between;
	margin-top: 4px;
	font-size: var(--text-xs, 12px);
	color: var(--red-600);
}
.db-error {
	min-width: 0;
	overflow-wrap: anywhere;
}
</style>
