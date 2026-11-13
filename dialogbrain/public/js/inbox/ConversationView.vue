<template>
	<div v-if="!store.selected" class="db-view-empty">{{ __("Select a conversation") }}</div>
	<div v-else-if="!conv" class="db-view-empty">{{ __("Loading…") }}</div>
	<div v-else class="db-conv">
		<ConversationHeader :conv="conv" />
		<div ref="scroller" class="db-messages" @scroll.passive="onScroll" @load.capture="onMediaLoad">
			<div class="db-earlier">
				<button v-if="box && box.nextBefore" type="button" class="btn btn-xs btn-default" :disabled="box.loading" @click="loadEarlier">
					{{ box.loading ? __("Loading…") : __("Load earlier") }}
				</button>
				<span v-else-if="box && box.loaded && !rows.length" class="db-muted">{{ __("No messages yet") }}</span>
				<span v-else-if="!box || !box.loaded" class="db-muted">{{ __("Loading…") }}</span>
			</div>
			<template v-for="item in items" :key="item.key">
				<div v-if="item.day" class="db-day"><span>{{ item.day }}</span></div>
				<MessageBubble v-else :m="item.m" :conversation="conv.name" />
			</template>
		</div>
		<DraftBar :conversation="conv.name" @edit="composer && composer.focus()" />
		<Composer ref="composer" :conversation="conv.name" />
	</div>
</template>

<script setup>
import { ref, computed, watch, nextTick } from "vue";
import ConversationHeader from "./ConversationHeader.vue";
import MessageBubble from "./MessageBubble.vue";
import DraftBar from "./DraftBar.vue";
import Composer from "./Composer.vue";
import { store, loadMessages } from "./store.js";
import { dayKey, dayLabel } from "./utils.js";

const scroller = ref(null);
const composer = ref(null);
const conv = computed(() => store.conversation);
const box = computed(() => (store.selected ? store.messages[store.selected] : null));
const rows = computed(() => (box.value ? box.value.rows : []));

// rows with a day separator before the first message of each day
const items = computed(() => {
	const out = [];
	let last = null;
	for (const m of rows.value) {
		const k = m._optimistic ? last : dayKey(m.sent_at);
		if (k && k !== last) {
			out.push({ key: "day-" + k, day: dayLabel(m.sent_at) });
			last = k;
		}
		out.push({ key: m._optimistic ? "tmp-" + m.client_ref : m.name, m });
	}
	return out;
});

// --- scrolling ----------------------------------------------------------------------
const NEAR = 120;
let stick = true; // the user is at (or near) the bottom
let preserveFrom = null; // scrollHeight before "Load earlier"

function atBottom() {
	const el = scroller.value;
	return !el || el.scrollHeight - el.scrollTop - el.clientHeight < NEAR;
}
function toBottom() {
	const el = scroller.value;
	if (el) el.scrollTop = el.scrollHeight;
}
function onScroll() {
	stick = atBottom();
}
function onMediaLoad() {
	if (stick) toBottom();
}

async function loadEarlier() {
	const el = scroller.value;
	preserveFrom = el ? { height: el.scrollHeight, top: el.scrollTop } : null;
	await loadMessages(store.selected, true);
}

// a new conversation: jump to the bottom once its messages are in
watch(
	() => store.selected,
	() => {
		stick = true;
		nextTick(toBottom);
	}
);

watch(
	() => rows.value.length && rows.value[rows.value.length - 1],
	() => {
		nextTick(() => {
			const el = scroller.value;
			if (!el || preserveFrom) return;
			const last = rows.value[rows.value.length - 1];
			// own optimistic message: always follow it; otherwise only when near the bottom
			if (stick || (last && last._optimistic)) toBottom();
			stick = atBottom();
		});
	},
	{ flush: "post" }
);

watch(
	() => rows.value.length && rows.value[0],
	() => {
		if (!preserveFrom) return;
		nextTick(() => {
			const el = scroller.value;
			if (el && preserveFrom) el.scrollTop = preserveFrom.top + (el.scrollHeight - preserveFrom.height);
			preserveFrom = null;
		});
	},
	{ flush: "post" }
);
</script>

<style scoped>
.db-view-empty {
	height: 100%;
	display: flex;
	align-items: center;
	justify-content: center;
	color: var(--text-muted);
	padding: 16px;
}
.db-conv {
	display: flex;
	flex-direction: column;
	height: 100%;
	min-height: 0;
}
.db-messages {
	flex: 1 1 auto;
	min-height: 0;
	overflow-y: auto;
	padding: 8px 12px 12px;
	background: var(--bg-color);
}
.db-earlier {
	text-align: center;
	padding: 4px 0 8px;
}
.db-muted {
	color: var(--text-muted);
	font-size: var(--text-xs, 12px);
}
.db-day {
	text-align: center;
	margin: 10px 0 6px;
}
.db-day span {
	display: inline-block;
	padding: 1px 10px;
	border-radius: 999px;
	background: var(--control-bg);
	color: var(--text-muted);
	font-size: var(--text-xs, 12px);
}
</style>
