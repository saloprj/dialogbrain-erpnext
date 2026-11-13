<template>
	<div class="db-inbox" :class="layoutClass">
		<aside class="db-list" v-show="width >= 768 || !store.selected">
			<ConversationList />
		</aside>
		<main class="db-view" v-show="width >= 768 || store.selected">
			<div class="db-toolbar" v-if="width < 768 || width < 1200">
				<button class="btn btn-xs btn-default" v-if="width < 768" @click="select(null)">
					{{ __("Back") }}
				</button>
				<button class="btn btn-xs btn-default" v-if="width < 1200" @click="store.cardOpen = !store.cardOpen">
					{{ __("Customer") }}
				</button>
			</div>
			<ConversationView />
		</main>
		<aside class="db-card" v-show="width >= 1200 || (store.cardOpen && width >= 768)" :class="{ drawer: width < 1200 }">
			<CustomerCard />
		</aside>
	</div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from "vue";
import ConversationList from "./ConversationList.vue";
import ConversationView from "./ConversationView.vue";
import CustomerCard from "./CustomerCard.vue";
import { store, select } from "./store.js";

const props = defineProps({ opts: { type: Object, default: () => ({}) } });
const width = ref(window.innerWidth);
const onResize = () => (width.value = window.innerWidth);
const layoutClass = computed(() =>
	width.value >= 1200 ? "wide" : width.value >= 768 ? "medium" : "narrow"
);
onMounted(() => {
	window.addEventListener("resize", onResize);
	if (props.opts.conversation) select(props.opts.conversation);
});
onBeforeUnmount(() => window.removeEventListener("resize", onResize));
</script>

<style scoped>
.db-inbox {
	display: flex;
	height: calc(100vh - 140px);
	min-height: 400px;
	border: 1px solid var(--border-color);
	border-radius: var(--border-radius-md, 8px);
	background: var(--bg-color);
	color: var(--text-color);
	overflow: hidden;
	position: relative;
}
.db-list { width: 360px; flex: 0 0 360px; border-right: 1px solid var(--border-color); overflow: auto; }
.db-view { flex: 1 1 auto; min-width: 0; overflow: auto; }
.db-card { width: 320px; flex: 0 0 320px; border-left: 1px solid var(--border-color); overflow: auto; }
.db-card.drawer { position: absolute; right: 0; top: 0; bottom: 0; background: var(--card-bg, var(--bg-color)); box-shadow: var(--shadow-lg, 0 4px 16px rgba(0,0,0,.2)); z-index: 5; }
.db-toolbar { display: flex; gap: 8px; padding: 8px; border-bottom: 1px solid var(--border-color); justify-content: space-between; }
.narrow .db-list { width: 100%; flex-basis: 100%; border-right: 0; }
</style>
