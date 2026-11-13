<template>
	<div class="db-list-pane">
		<ListFilters />
		<div class="db-rows" ref="scroller" @scroll.passive="maybeLoadMore">
			<ConversationRow
				v-for="row in store.rows"
				:key="row.name"
				:row="row"
				:active="row.name === store.selected"
				@open="select"
			/>
			<div v-if="store.listLoaded && !store.rows.length && !store.listLoading" class="db-list-empty">
				{{ __("No conversations") }}
			</div>
			<div v-if="store.listLoading" class="db-list-empty">{{ __("Loading…") }}</div>
		</div>
	</div>
</template>

<script setup>
import { ref, watch, nextTick, onMounted } from "vue";
import ListFilters from "./ListFilters.vue";
import ConversationRow from "./ConversationRow.vue";
import { store, select, loadList, loadCounters, loadListMeta } from "./store.js";

const scroller = ref(null);

// Infinite scroll on next_cursor: load more near the bottom, and keep loading while the
// rows do not fill the pane yet.
function maybeLoadMore() {
	const el = scroller.value;
	if (!el || !el.clientHeight || !store.nextCursor || store.listLoading) return;
	if (el.scrollTop + el.clientHeight >= el.scrollHeight - 300) loadList(true);
}

watch(
	() => JSON.stringify(store.filters),
	() => {
		if (scroller.value) scroller.value.scrollTop = 0;
		loadList();
	}
);
watch(
	() => store.listLoading,
	(loading) => !loading && nextTick(maybeLoadMore)
);

onMounted(() => {
	loadList();
	loadCounters();
	loadListMeta();
});
</script>

<style scoped>
.db-list-pane {
	display: flex;
	flex-direction: column;
	height: 100%;
	min-height: 0;
}
.db-rows {
	flex: 1 1 auto;
	overflow-y: auto;
	min-height: 0;
}
.db-list-empty {
	padding: 16px;
	color: var(--text-muted);
	text-align: center;
	font-size: var(--text-sm, 12px);
}
</style>
