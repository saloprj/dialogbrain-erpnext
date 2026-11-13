<template>
	<div class="db-header">
		<div class="db-header-main">
			<div class="db-header-name" :title="name">{{ name }}</div>
			<div class="db-header-sub">
				<span>{{ channelLabel(conv.channel && conv.channel.channel_type) }}</span>
				<span v-if="conv.channel && conv.channel.title">· {{ conv.channel.title }}</span>
				<span v-if="conv.phone">· {{ conv.phone }}</span>
				<span v-else-if="conv.email">· {{ conv.email }}</span>
				<a v-if="conv.party" class="db-party-chip" :href="formLink(conv.party_type, conv.party)" :title="conv.party_type">
					{{ __(conv.party_type) }}: {{ conv.party }}
				</a>
				<span v-if="conv.needs_human" class="db-human">{{ __("Needs human") }}</span>
				<span v-if="agent && agent.paused" class="db-agent-paused" :title="agentPausedTitle">
					{{ agent.paused_by === "operator" ? __("Taken over") : __("Agent paused") }}
				</span>
			</div>
		</div>
		<div class="db-header-actions">
			<div class="dropdown">
				<button
					type="button"
					class="btn btn-xs btn-default dropdown-toggle db-assign"
					data-toggle="dropdown"
					aria-haspopup="true"
					aria-expanded="false"
					:disabled="busy || (!canAssignAny && !agentOptions.length)"
					@click="onAssignOpen"
				>
					<span class="db-assign-label">{{ conv.assigned_to ? conv.assigned_to_name || conv.assigned_to : __("Unassigned") }}</span>
				</button>
				<div class="dropdown-menu dropdown-menu-right db-assign-menu">
					<template v-if="canAssignAny">
						<div v-if="users === null" class="dropdown-item disabled">{{ __("Loading…") }}</div>
						<a
							v-for="u in users || []"
							:key="u.name"
							class="dropdown-item"
							:class="{ active: u.name === conv.assigned_to }"
							href="#"
							@click.prevent="assign(u.name)"
						>
							{{ u.full_name }}<span v-if="u.name === me"> ({{ __("me") }})</span>
						</a>
						<template v-if="conv.assigned_to">
							<div class="dropdown-divider"></div>
							<a class="dropdown-item" href="#" @click.prevent="assign(null)">{{ __("Unassign") }}</a>
						</template>
					</template>
					<template v-else>
						<a v-for="o in agentOptions" :key="o.label" class="dropdown-item" href="#" @click.prevent="assign(o.user)">
							{{ o.label }}
						</a>
					</template>
				</div>
			</div>
			<template v-if="canPause">
				<!-- R3: "taken over" only for an operator pause; any pause can be given back -->
				<button
					v-if="!agent || !agent.paused || agent.paused_by !== 'operator'"
					type="button"
					class="btn btn-xs btn-default"
					:disabled="busy"
					:title="__('Stop the AI agent answering this conversation until you give it back')"
					@click="setAgent(true)"
				>
					{{ __("Take over") }}
				</button>
				<button v-if="agent && agent.paused" type="button" class="btn btn-xs btn-default" :disabled="busy" @click="setAgent(false)">
					{{ __("Give back to agent") }}
				</button>
			</template>
			<button type="button" class="btn btn-xs btn-default" :disabled="busy" @click="toggleStatus">
				{{ conv.status === "Closed" ? __("Reopen") : __("Close") }}
			</button>
		</div>
	</div>
</template>

<script setup>
import { ref, computed, watch, onBeforeUnmount } from "vue";
import { api } from "./api.js";
import { patchRow, loadCounters } from "./store.js";
import { channelLabel, formLink } from "./utils.js";

const props = defineProps({ conv: { type: Object, required: true } });

const me = frappe.session.user;
const busy = ref(false);
const users = ref(null);
const name = computed(() => props.conv.contact_name || props.conv.title || props.conv.name);
const canAssignAny = computed(() => !!(props.conv.permissions && props.conv.permissions.can_assign_any));
// Take over / give back: only when the connected DialogBrain supports it (Plan 3).
const canPause = computed(() => !!(props.conv.permissions && props.conv.permissions.agent_pause));
const agent = ref(null); // {paused, paused_by, paused_at}; null = not known
const agentPausedTitle = computed(() =>
	agent.value && agent.value.paused_by === "operator"
		? __("A person took this conversation over: the AI agent does not answer it")
		: __("The AI agent is paused in this conversation (after a manual reply or an escalation)")
);
const agentOptions = computed(() => {
	const a = props.conv.assigned_to;
	if (!a) return [{ label: __("Assign to me"), user: me }];
	if (a === me) return [{ label: __("Unassign"), user: null }];
	return [];
});

watch(
	() => props.conv.name,
	() => (users.value = null)
);

let agentSeq = 0;
let agentTimer = null;
async function loadAgent() {
	const conv = props.conv.name;
	const seq = ++agentSeq;
	if (!canPause.value) {
		agent.value = null;
		return;
	}
	try {
		const r = await api.agentStatus(conv);
		if (seq === agentSeq && conv === props.conv.name) agent.value = r && r.supported && !r.error ? r : null;
	} catch (e) {
		if (seq === agentSeq) agent.value = null;
	}
}

// A take-over elsewhere (agent_override) or a new message (a manual reply pauses the agent)
// can change the agent's state: re-read it, debounced.
watch(
	() => [props.conv.name, canPause.value, props.conv.agent_override, props.conv.last_message_at],
	(now, before) => {
		if (!before || now[0] !== before[0]) agent.value = null;
		clearTimeout(agentTimer);
		agentTimer = setTimeout(loadAgent, before && now[0] === before[0] ? 800 : 0);
	},
	{ immediate: true }
);
onBeforeUnmount(() => clearTimeout(agentTimer));

async function setAgent(paused) {
	const conv = props.conv.name;
	busy.value = true;
	try {
		const r = await (paused ? api.takeOver(conv) : api.giveBack(conv));
		if (r) {
			const { agent: state, ...row } = r;
			patchRow(row);
			if (conv === props.conv.name) {
				agentSeq++; // a status read already in flight is older than this
				agent.value = state;
			}
		}
	} catch (e) {
		// Frappe showed the error
	} finally {
		busy.value = false;
	}
}

async function onAssignOpen() {
	if (!canAssignAny.value || users.value !== null) return;
	const conv = props.conv.name;
	try {
		const rows = await api.assignableUsers(conv);
		if (conv === props.conv.name) users.value = rows || [];
	} catch (e) {
		users.value = [];
	}
}

async function run(fn) {
	busy.value = true;
	try {
		patchRow(await fn());
		loadCounters();
	} catch (e) {
		// Frappe showed the error
	} finally {
		busy.value = false;
	}
}

function assign(user) {
	if ((props.conv.assigned_to || null) === (user || null)) return;
	run(() => api.assign(props.conv.name, user));
}

function toggleStatus() {
	run(() => api.setStatus(props.conv.name, props.conv.status === "Closed" ? "Open" : "Closed"));
}
</script>

<style scoped>
.db-header {
	display: flex;
	align-items: center;
	gap: 8px;
	padding: 8px 12px;
	border-bottom: 1px solid var(--border-color);
	background: var(--fg-color);
	flex-wrap: wrap;
}
.db-header-main {
	flex: 1 1 200px;
	min-width: 0;
}
.db-header-name {
	font-weight: 600;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.db-header-sub {
	display: flex;
	flex-wrap: wrap;
	gap: 4px;
	align-items: center;
	font-size: var(--text-xs, 12px);
	color: var(--text-muted);
}
.db-party-chip {
	margin-left: 4px;
	padding: 0 8px;
	border-radius: 999px;
	background: var(--bg-blue);
	color: var(--text-color);
	text-decoration: none;
}
.db-human {
	margin-left: 4px;
	padding: 0 8px;
	border-radius: 999px;
	background: var(--bg-red);
	color: var(--red-600);
}
.db-agent-paused {
	margin-left: 4px;
	padding: 0 8px;
	border-radius: 999px;
	background: var(--bg-yellow);
	color: var(--yellow-700, var(--text-color));
}
.db-header-actions {
	display: flex;
	gap: 6px;
	align-items: center;
	flex: 0 0 auto;
}
.db-assign-label {
	display: inline-block;
	max-width: 160px;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	vertical-align: bottom;
}
.db-assign-menu {
	max-height: 320px;
	overflow-y: auto;
}
</style>
