import { reactive } from "vue";

export const store = reactive({
	selected: null, // conversation name
	filters: { status: "Open" },
	rows: [],
	nextCursor: null,
	messages: {}, // conversation -> message list
	counters: {},
	cardOpen: false,
});

export function select(name) {
	store.selected = name || null;
}
