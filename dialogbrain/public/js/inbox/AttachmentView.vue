<template>
	<div class="db-att" :class="'db-att-' + kind">
		<template v-if="att._local">
			<span class="db-att-file">📎 {{ att.filename || __("File") }}</span>
		</template>
		<template v-else-if="kind === 'image' && !failed">
			<button type="button" class="db-att-thumb" :title="att.filename || __('Image')" @click="open = true">
				<img :src="url" :alt="att.filename || __('Image')" loading="lazy" @error="failed = true" />
			</button>
			<Teleport to="body">
				<div v-if="open" class="db-lightbox" role="dialog" :aria-label="att.filename || __('Image')" @click="open = false">
					<img :src="url" :alt="att.filename || __('Image')" />
					<div class="db-lightbox-bar" @click.stop>
						<a class="btn btn-xs btn-default" :href="url" target="_blank" rel="noopener">{{ __("Open original") }}</a>
						<button type="button" class="btn btn-xs btn-default" @click="open = false">{{ __("Close") }}</button>
					</div>
				</div>
			</Teleport>
		</template>
		<video v-else-if="kind === 'video'" :src="url" controls preload="metadata"></video>
		<audio v-else-if="kind === 'audio'" :src="url" controls preload="none"></audio>
		<a v-else class="db-att-file" :href="url" target="_blank" rel="noopener" download>
			📎 {{ att.filename || __("Download file") }}
			<span v-if="att.size" class="db-att-size">{{ size }}</span>
		</a>
	</div>
</template>

<script setup>
import { ref, computed, watch, onBeforeUnmount } from "vue";
import { api } from "./api.js";
import { attachmentKind } from "./utils.js";

const props = defineProps({
	message: { type: String, required: true },
	att: { type: Object, required: true },
});

const open = ref(false);
const failed = ref(false);
const kind = computed(() => (props.att._local ? "file" : attachmentKind(props.att)));
const url = computed(() => (props.att.file_id ? api.mediaUrl(props.message, props.att.file_id) : ""));
const size = computed(() => {
	const n = Number(props.att.size) || 0;
	if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
	if (n >= 1024) return Math.round(n / 1024) + " KB";
	return n + " B";
});

const onKey = (e) => e.key === "Escape" && (open.value = false);
watch(open, (v) => (v ? document.addEventListener("keydown", onKey) : document.removeEventListener("keydown", onKey)));
onBeforeUnmount(() => document.removeEventListener("keydown", onKey));
</script>

<style scoped>
.db-att {
	margin-top: 4px;
	max-width: 100%;
}
.db-att-thumb {
	display: block;
	padding: 0;
	border: 0;
	background: none;
	cursor: zoom-in;
	max-width: 100%;
}
.db-att-thumb img {
	display: block;
	max-width: min(100%, 260px);
	max-height: 260px;
	border-radius: var(--border-radius, 6px);
	object-fit: cover;
}
.db-att video {
	display: block;
	max-width: min(100%, 320px);
	max-height: 260px;
	border-radius: var(--border-radius, 6px);
}
.db-att audio {
	display: block;
	max-width: 100%;
	width: 260px;
}
.db-att-file {
	display: block;
	max-width: 100%;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	font-size: var(--text-sm, 12px);
	color: inherit;
	text-decoration: underline;
}
.db-att-size {
	color: var(--text-muted);
	margin-left: 4px;
	text-decoration: none;
}
.db-lightbox {
	position: fixed;
	inset: 0;
	z-index: 1050;
	background: color-mix(in srgb, var(--gray-900) 88%, transparent);
	display: flex;
	align-items: center;
	justify-content: center;
	cursor: zoom-out;
	padding: 16px;
}
.db-lightbox img {
	max-width: 100%;
	max-height: calc(100% - 48px);
	object-fit: contain;
}
.db-lightbox-bar {
	position: absolute;
	top: 12px;
	right: 12px;
	display: flex;
	gap: 6px;
	cursor: default;
}
</style>
