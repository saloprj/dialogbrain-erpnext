<template>
	<button
		type="button"
		class="db-row"
		:class="{ active, unread: row.unread_count > 0 }"
		:aria-current="active ? 'true' : null"
		@click="$emit('open', row.name)"
	>
		<span class="db-avatar" :title="name">
			<img v-if="avatar && !avatarFailed" :src="avatar" alt="" @error="avatarFailed = true" />
			<span v-else>{{ initials(name) }}</span>
			<span v-if="row.needs_human" class="db-human-dot" :title="__('Needs human')"></span>
		</span>
		<span class="db-row-main">
			<span class="db-row-top">
				<span class="db-row-name">{{ name }}</span>
				<span class="db-row-time">{{ shortTime(row.last_message_at) }}</span>
			</span>
			<span class="db-row-meta">
				<span class="db-channel">{{ channelLabel(row.channel_type) }}</span>
				<span v-if="row.assigned_to" class="db-assignee">· {{ row.assigned_to_name || row.assigned_to }}</span>
				<span v-if="row.needs_human" class="db-human-label">· {{ __("Needs human") }}</span>
			</span>
			<span class="db-row-bottom">
				<span class="db-preview">
					<span v-if="row.last_message_direction === 'out'" class="db-you">{{ __("You") }}: </span>{{ row.last_message_preview }}
				</span>
				<span v-if="row.unread_count > 0" class="db-unread">{{ row.unread_count > 99 ? "99+" : row.unread_count }}</span>
			</span>
		</span>
	</button>
</template>

<script setup>
import { ref, computed } from "vue";
import { channelLabel, initials, shortTime } from "./utils.js";

const props = defineProps({ row: { type: Object, required: true }, active: Boolean });
defineEmits(["open"]);

const avatarFailed = ref(false);
const name = computed(() => props.row.contact_name || props.row.title || props.row.name);
const avatar = computed(() => {
	const url = props.row.avatar_url || "";
	return /^(https:\/\/|\/)/.test(url) ? url : null;
});
</script>

<style scoped>
.db-row {
	display: flex;
	gap: 10px;
	width: 100%;
	padding: 10px 12px;
	border: 0;
	border-bottom: 1px solid var(--border-color);
	background: transparent;
	color: var(--text-color);
	text-align: left;
	cursor: pointer;
	align-items: flex-start;
}
.db-row:hover {
	background: var(--bg-light-gray, var(--subtle-fg));
}
.db-row.active {
	background: var(--bg-gray, var(--subtle-accent));
}
.db-avatar {
	position: relative;
	flex: 0 0 36px;
	width: 36px;
	height: 36px;
	border-radius: 50%;
	background: var(--bg-gray, var(--control-bg));
	color: var(--text-muted);
	display: flex;
	align-items: center;
	justify-content: center;
	font-size: var(--text-xs, 11px);
	font-weight: 600;
}
.db-avatar img {
	width: 100%;
	height: 100%;
	border-radius: 50%;
	object-fit: cover;
}
.db-human-dot {
	position: absolute;
	top: -1px;
	right: -1px;
	width: 11px;
	height: 11px;
	border-radius: 50%;
	background: var(--red-500, var(--danger));
	border: 2px solid var(--fg-color, var(--bg-color));
}
.db-row-main {
	flex: 1 1 auto;
	min-width: 0;
	display: flex;
	flex-direction: column;
	gap: 1px;
}
.db-row-top,
.db-row-bottom {
	display: flex;
	gap: 6px;
	align-items: baseline;
}
.db-row-name {
	flex: 1 1 auto;
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	font-weight: 500;
}
.unread .db-row-name {
	font-weight: 700;
}
.db-row-time {
	flex: 0 0 auto;
	font-size: var(--text-xs, 11px);
	color: var(--text-muted);
}
.db-row-meta {
	font-size: var(--text-xs, 11px);
	color: var(--text-muted);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.db-human-label {
	color: var(--red-600, var(--danger));
}
.db-preview {
	flex: 1 1 auto;
	min-width: 0;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	font-size: var(--text-sm, 12px);
	color: var(--text-muted);
}
.unread .db-preview {
	color: var(--text-color);
}
.db-you {
	color: var(--text-light, var(--text-muted));
}
.db-unread {
	flex: 0 0 auto;
	min-width: 18px;
	height: 18px;
	padding: 0 5px;
	border-radius: 9px;
	background: var(--primary, var(--blue-500));
	color: var(--fg-color);
	font-size: 10px;
	font-weight: 600;
	line-height: 18px;
	text-align: center;
}
</style>
