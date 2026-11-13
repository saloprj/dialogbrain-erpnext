<template>
	<div class="db-filters">
		<div class="db-filter-row">
			<input
				class="form-control input-xs db-search"
				type="search"
				:placeholder="__('Search name, phone, text…')"
				:aria-label="__('Search conversations')"
				v-model="search"
			/>
			<select class="form-control input-xs db-select" v-model="store.filters.status" :aria-label="__('Status')">
				<option value="Open">{{ __("Open") }}</option>
				<option value="Closed">{{ __("Closed") }}</option>
				<option value="All">{{ __("All") }}</option>
			</select>
			<select
				v-if="store.managers.length"
				class="form-control input-xs db-select"
				v-model="store.filters.assigned_to"
				:aria-label="__('Assigned to')"
			>
				<option :value="null">{{ __("Anyone") }}</option>
				<option v-for="u in store.managers" :key="u.name" :value="u.name">{{ u.full_name }}</option>
			</select>
		</div>
		<div class="db-chips" role="toolbar" :aria-label="__('Channel')">
			<button class="db-chip" :class="{ active: allActive }" @click="resetChips">{{ __("All") }}</button>
			<button
				v-for="t in store.channelTypes"
				:key="t"
				class="db-chip"
				:class="{ active: store.filters.channel_type === t }"
				@click="store.filters.channel_type = store.filters.channel_type === t ? null : t"
			>
				{{ channelLabel(t) }}
			</button>
		</div>
		<div class="db-chips db-chips-wrap" role="toolbar" :aria-label="__('Quick filters')">
			<button class="db-chip" :class="{ active: store.filters.unanswered }" @click="store.filters.unanswered = !store.filters.unanswered">
				{{ __("Unanswered") }} <span class="db-count">{{ c.unanswered || 0 }}</span>
			</button>
			<button class="db-chip" :class="{ active: store.filters.scope === 'mine' }" @click="toggleScope('mine')">
				{{ __("Mine") }} <span class="db-count">{{ c.mine || 0 }}</span>
			</button>
			<button class="db-chip" :class="{ active: store.filters.scope === 'unassigned' }" @click="toggleScope('unassigned')">
				{{ __("Unassigned") }} <span class="db-count">{{ c.unassigned || 0 }}</span>
			</button>
			<button
				class="db-chip db-chip-alert"
				:class="{ active: store.filters.needs_human }"
				@click="store.filters.needs_human = !store.filters.needs_human"
			>
				{{ __("Needs human") }} <span class="db-count">{{ c.needs_human || 0 }}</span>
			</button>
		</div>
	</div>
</template>

<script setup>
import { ref, computed, watch, onBeforeUnmount } from "vue";
import { store } from "./store.js";
import { channelLabel, debounce } from "./utils.js";

const c = computed(() => store.counters || {});
const search = ref(store.filters.search);
const applySearch = debounce((v) => (store.filters.search = v), 300);
watch(search, (v) => applySearch(v));
onBeforeUnmount(() => applySearch.cancel());

const allActive = computed(() => {
	const f = store.filters;
	return !f.channel_type && f.scope === "all" && !f.unanswered && !f.needs_human;
});

function resetChips() {
	Object.assign(store.filters, { channel_type: null, scope: "all", unanswered: false, needs_human: false });
}

function toggleScope(scope) {
	store.filters.scope = store.filters.scope === scope ? "all" : scope;
}
</script>

<style scoped>
.db-filters {
	padding: var(--padding-sm, 8px);
	border-bottom: 1px solid var(--border-color);
	display: flex;
	flex-direction: column;
	gap: 6px;
	background: var(--fg-color);
}
.db-filter-row {
	display: flex;
	gap: 6px;
}
.db-search {
	flex: 1 1 auto;
	min-width: 0;
}
.db-select {
	flex: 0 0 auto;
	width: auto;
	max-width: 120px;
}
.db-chips {
	display: flex;
	gap: 4px;
	overflow-x: auto;
	scrollbar-width: none;
}
.db-chips-wrap {
	flex-wrap: wrap;
	overflow: visible;
}
.db-chips::-webkit-scrollbar {
	display: none;
}
.db-chip {
	flex: 0 0 auto;
	border: 1px solid var(--border-color);
	background: var(--control-bg, var(--bg-color));
	color: var(--text-color);
	border-radius: 999px;
	padding: 2px 10px;
	font-size: var(--text-xs, 11px);
	line-height: 18px;
	white-space: nowrap;
	cursor: pointer;
}
.db-chip:hover {
	background: var(--bg-gray, var(--control-bg));
}
.db-chip.active {
	background: var(--text-color);
	border-color: var(--text-color);
	color: var(--bg-color);
}
.db-count {
	opacity: 0.7;
	margin-left: 2px;
}
.db-chip-alert:not(.active) .db-count {
	color: var(--red-600, var(--text-color));
	opacity: 1;
}
</style>
