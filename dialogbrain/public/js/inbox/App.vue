<template>
	<div ref="root" class="db-inbox" :class="layout" :style="{ height: height }">
		<aside class="db-list" v-show="layout !== 'narrow' || !store.selected">
			<ConversationList />
		</aside>
		<main class="db-view" v-show="layout !== 'narrow' || store.selected">
			<div class="db-toolbar" v-if="layout !== 'wide' && store.selected">
				<button v-if="layout === 'narrow'" type="button" class="btn btn-xs btn-default" @click="select(null)">
					← {{ __("Back") }}
				</button>
				<span v-else></span>
				<button type="button" class="btn btn-xs btn-default" :class="{ active: store.cardOpen }" @click="store.cardOpen = !store.cardOpen">
					{{ __("Customer") }}
				</button>
			</div>
			<div class="db-view-body">
				<ConversationView />
			</div>
		</main>
		<aside
			class="db-card"
			v-show="layout === 'wide' || (store.cardOpen && store.selected)"
			:class="{ 'db-drawer': layout === 'medium', 'db-overlay': layout === 'narrow' }"
		>
			<CustomerCard :closable="layout !== 'wide'" @close="store.cardOpen = false" />
		</aside>
	</div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, nextTick } from "vue";
import ConversationList from "./ConversationList.vue";
import ConversationView from "./ConversationView.vue";
import CustomerCard from "./CustomerCard.vue";
import { store, select, onMessageEvent, onMessageRemoved, onDraftEvent } from "./store.js";

const props = defineProps({ opts: { type: Object, default: () => ({}) } });
const root = ref(null);
const width = ref(window.innerWidth);
const top = ref(0);

const layout = computed(() => (width.value >= 1200 ? "wide" : width.value >= 768 ? "medium" : "narrow"));
// Fill the viewport below wherever Frappe's page chrome ends (measured, not a magic number).
const height = computed(() => `calc(100dvh - ${Math.max(top.value, 0)}px - var(--padding-md, 15px))`);

function measure() {
	width.value = window.innerWidth;
	if (root.value) top.value = Math.round(root.value.getBoundingClientRect().top + window.scrollY);
}

const events = {
	"dialogbrain:message": onMessageEvent,
	"dialogbrain:message_updated": onMessageEvent,
	"dialogbrain:message_removed": onMessageRemoved,
	"dialogbrain:draft": onDraftEvent,
};

onMounted(() => {
	window.addEventListener("resize", measure);
	nextTick(measure);
	for (const [name, fn] of Object.entries(events)) frappe.realtime.on(name, fn);
	if (props.opts.conversation) select(props.opts.conversation, { updateUrl: false });
});

onBeforeUnmount(() => {
	window.removeEventListener("resize", measure);
	for (const [name, fn] of Object.entries(events)) frappe.realtime.off(name, fn);
});

defineExpose({ measure });
</script>

<style scoped>
.db-inbox {
	display: flex;
	min-height: 420px;
	border: 1px solid var(--border-color);
	border-radius: var(--border-radius-md, 8px);
	background: var(--fg-color);
	color: var(--text-color);
	overflow: hidden;
	position: relative;
}
.db-list {
	width: 360px;
	flex: 0 0 360px;
	border-right: 1px solid var(--border-color);
	min-height: 0;
}
.db-view {
	flex: 1 1 auto;
	min-width: 0;
	display: flex;
	flex-direction: column;
	min-height: 0;
}
.db-view-body {
	flex: 1 1 auto;
	min-height: 0;
}
.db-card {
	width: 320px;
	flex: 0 0 320px;
	border-left: 1px solid var(--border-color);
	min-height: 0;
	background: var(--fg-color);
}
.db-card.db-drawer {
	position: absolute;
	right: 0;
	top: 0;
	bottom: 0;
	box-shadow: var(--shadow-lg);
	z-index: 5;
}
/* below 768 px the card is a full-screen overlay with its own Close button */
.db-card.db-overlay {
	position: fixed;
	inset: 0;
	width: auto;
	border-left: 0;
	z-index: 1040;
}
.db-toolbar {
	display: flex;
	gap: 8px;
	padding: 6px 8px;
	border-bottom: 1px solid var(--border-color);
	justify-content: space-between;
	background: var(--fg-color);
}
.narrow .db-list {
	width: 100%;
	flex-basis: 100%;
	border-right: 0;
}
</style>
