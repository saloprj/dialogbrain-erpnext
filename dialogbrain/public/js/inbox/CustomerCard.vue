<template>
	<div class="db-card-pane">
		<div class="db-card-head">
			<span class="db-card-title">{{ __("Customer") }}</span>
			<button
				v-if="closable"
				type="button"
				class="btn btn-xs btn-default"
				:aria-label="__('Close')"
				@click="$emit('close')"
			>
				{{ __("Close") }}
			</button>
		</div>
		<div v-if="!store.selected" class="db-muted db-pad">{{ __("Select a conversation") }}</div>
		<div v-else-if="!card" class="db-muted db-pad">{{ __("Loading…") }}</div>
		<div v-else class="db-card-body">
			<!-- from chat: what the channel sent (always shown) -->
			<section class="db-section">
				<div class="db-section-title">{{ __("From chat") }}</div>
				<div class="db-contact-name">{{ chatName }}</div>
				<div v-if="card.chat.handle" class="db-line db-muted">{{ card.chat.handle }}</div>
				<div v-if="card.chat.phone" class="db-line">
					<a :href="'tel:' + card.chat.phone">{{ card.chat.phone }}</a>
				</div>
				<div v-if="card.chat.email" class="db-line">
					<a :href="'mailto:' + card.chat.email">{{ card.chat.email }}</a>
				</div>
			</section>

			<!-- not linked -->
			<section v-if="!card.party" class="db-section">
				<div class="db-section-title">{{ __("Not linked to a customer") }}</div>
				<template v-if="card.has_access.party">
				<div v-if="card.suggestions.length" class="db-sub">{{ __("Suggestions") }}</div>
				<div v-for="s in card.suggestions" :key="s.party_type + s.party" class="db-pick">
					<div class="db-pick-main">
						<div class="db-pick-name">{{ s.party_name || s.party }}</div>
						<div class="db-muted db-small">{{ __(s.party_type) }} · {{ __("matched by {0}", [__(s.matched_by)]) }}</div>
					</div>
					<button type="button" class="btn btn-xs btn-default" :disabled="busy" @click="link(s.party_type, s.party)">
						{{ __("Link") }}
					</button>
				</div>
				<div class="db-actions">
					<button type="button" class="btn btn-xs btn-default" :disabled="busy" @click="searchOpen = !searchOpen">
						{{ __("Link to existing…") }}
					</button>
					<button type="button" class="btn btn-xs btn-default" :disabled="busy" @click="createLead">
						{{ __("Create Lead") }}
					</button>
				</div>
				<div v-if="searchOpen" class="db-search-box">
					<input
						ref="searchInput"
						class="form-control input-xs"
						type="search"
						:placeholder="__('Customer or lead name, phone, email')"
						v-model="query"
					/>
					<div v-if="query.trim().length >= 2 && results === null" class="db-muted db-small">{{ __("Searching…") }}</div>
					<div v-else-if="query.trim().length >= 2 && !results.length" class="db-muted db-small">{{ __("Nothing found") }}</div>
					<div v-for="r in results || []" :key="r.party_type + r.party" class="db-pick">
						<div class="db-pick-main">
							<div class="db-pick-name">{{ r.title || r.party }}</div>
							<div class="db-muted db-small">
								{{ __(r.party_type) }}<span v-if="r.phone"> · {{ r.phone }}</span><span v-if="r.email"> · {{ r.email }}</span>
							</div>
						</div>
						<button type="button" class="btn btn-xs btn-default" :disabled="busy" @click="link(r.party_type, r.party)">
							{{ __("Link") }}
						</button>
					</div>
				</div>
				</template>
				<div v-else class="db-muted db-small">{{ __("You have no access to customers or leads.") }}</div>
			</section>

			<!-- customer -->
			<section v-else-if="card.party_type === 'Customer'" class="db-section">
				<div class="db-section-title">{{ __("Customer") }}</div>
				<a v-if="card.has_access.party" class="db-party-name" :href="formLink('Customer', card.party)">{{ card.party_name || card.party }}</a>
				<div v-else class="db-muted db-small">{{ __("Linked to a customer you have no access to.") }}</div>
				<div v-if="card.link_source" class="db-muted db-small">{{ __("Linked: {0}", [__(card.link_source)]) }}</div>
				<template v-if="card.contact">
					<div class="db-sub">{{ __("Contact") }}</div>
					<div v-if="card.contact.phone" class="db-line">
						<a :href="'tel:' + card.contact.phone">{{ card.contact.phone }}</a>
					</div>
					<div v-if="card.contact.email" class="db-line">
						<a :href="'mailto:' + card.contact.email">{{ card.contact.email }}</a>
					</div>
					<div class="db-line">
						<a :href="formLink('Contact', card.contact.name)">{{ __("Open Contact") }}</a>
					</div>
				</template>
				<template v-if="card.has_access.orders && card.stats">
					<div class="db-stats">
						<div class="db-stat">
							<div class="db-stat-value">{{ card.stats.orders_count }}</div>
							<div class="db-muted db-small">{{ __("Orders") }}</div>
						</div>
						<div class="db-stat db-stat-totals">
							<div v-for="t in card.stats.totals" :key="t.currency || 'none'" class="db-total">
								<span class="db-stat-value">{{ money(t.total, t.currency) }}</span>
								<span class="db-muted db-small">· {{ t.count }} {{ t.count === 1 ? __("order") : __("orders") }}</span>
							</div>
							<div v-if="!card.stats.totals.length" class="db-muted db-small">{{ __("No orders yet") }}</div>
						</div>
					</div>
					<div v-if="card.recent_orders.length" class="db-sub">{{ __("Recent orders") }}</div>
					<a v-for="o in card.recent_orders" :key="o.name" class="db-doc" :href="formLink('Sales Order', o.name)">
						<span class="db-doc-name">{{ o.name }}</span>
						<span class="db-muted db-small">{{ date(o.transaction_date) }} · {{ __(o.status) }}</span>
						<span class="db-doc-amount">{{ money(o.grand_total, o.currency) }}</span>
					</a>
				</template>
				<template v-if="card.has_access.invoices">
					<div v-if="card.unpaid_invoices.length" class="db-sub">{{ __("Unpaid invoices") }}</div>
					<a v-for="i in card.unpaid_invoices" :key="i.name" class="db-doc" :href="formLink('Sales Invoice', i.name)">
						<span class="db-doc-name">{{ i.name }}</span>
						<span class="db-muted db-small">{{ __("Due {0}", [date(i.due_date)]) }}</span>
						<span class="db-doc-amount db-overdue">{{ money(i.outstanding_amount, i.currency) }}</span>
					</a>
				</template>
				<div class="db-actions">
					<button v-if="card.has_access.party && canCreate('Sales Order')" type="button" class="btn btn-xs btn-primary" @click="newDoc('Sales Order', { customer: card.party })">
						{{ __("Create Sales Order") }}
					</button>
					<button
						v-if="card.has_access.party && canCreate('Quotation')"
						type="button"
						class="btn btn-xs btn-default"
						@click="newDoc('Quotation', { quotation_to: 'Customer', party_name: card.party })"
					>
						{{ __("Create Quotation") }}
					</button>
				</div>
			</section>

			<!-- lead -->
			<section v-else-if="card.party_type === 'Lead'" class="db-section">
				<div class="db-section-title">{{ __("Lead") }}</div>
				<template v-if="card.has_access.party">
				<a class="db-party-name" :href="formLink('Lead', card.party)">{{ card.party_name || card.party }}</a>
				<div v-if="card.lead" class="db-small">
					<span v-if="card.lead.status" class="db-badge">{{ __(card.lead.status) }}</span>
					<span v-if="card.lead.company_name"> {{ card.lead.company_name }}</span>
				</div>
				<template v-if="card.contact">
					<div class="db-sub">{{ __("Contact") }}</div>
					<div v-if="card.contact.phone" class="db-line">
						<a :href="'tel:' + card.contact.phone">{{ card.contact.phone }}</a>
					</div>
					<div v-if="card.contact.email" class="db-line">
						<a :href="'mailto:' + card.contact.email">{{ card.contact.email }}</a>
					</div>
					<div class="db-line">
						<a :href="formLink('Contact', card.contact.name)">{{ __("Open Contact") }}</a>
					</div>
				</template>
				<div class="db-actions">
					<button
						v-if="canCreate('Quotation')"
						type="button"
						class="btn btn-xs btn-primary"
						@click="newDoc('Quotation', { quotation_to: 'Lead', party_name: card.party })"
					>
						{{ __("Create Quotation") }}
					</button>
					<button type="button" class="btn btn-xs btn-default" @click="openLead">
						{{ __("Open Lead") }}
					</button>
				</div>
				</template>
				<div v-else class="db-muted db-small">{{ __("Linked to a lead you have no access to.") }}</div>
			</section>

			<section class="db-section">
				<NotesPanel :conversation="store.selected" />
			</section>
		</div>
	</div>
</template>

<script setup>
import { ref, computed, watch, nextTick } from "vue";
import { api } from "./api.js";
import { store, refreshConversation } from "./store.js";
import NotesPanel from "./NotesPanel.vue";
import { debounce, formLink, money } from "./utils.js";

defineProps({ closable: Boolean });
defineEmits(["close"]);

const card = ref(null);
const busy = ref(false);
const searchOpen = ref(false);
const searchInput = ref(null);
const query = ref("");
const results = ref([]);

// "From chat" shows only what the channel sent; the party's Contact is a separate block
const chatName = computed(() => {
	const c = store.conversation;
	return (card.value && card.value.chat.contact_name) || (c && c.title) || "";
});

const date = (v) => (v ? frappe.datetime.str_to_user(v) : "");
const canCreate = (doctype) => frappe.model.can_create(doctype);

async function load() {
	const conv = store.selected;
	card.value = null;
	searchOpen.value = false;
	query.value = "";
	if (!conv) return;
	try {
		const data = await api.getCard(conv);
		if (conv === store.selected) card.value = data;
	} catch (e) {
		// Frappe showed the error
	}
}

watch(() => [store.selected, store.cardVersion], load, { immediate: true });

const search = debounce(async (txt) => {
	const conv = store.selected;
	try {
		const rows = await api.searchParties(conv, txt);
		if (conv === store.selected && txt === query.value.trim()) results.value = rows || [];
	} catch (e) {
		results.value = [];
	}
}, 300);

watch(query, (v) => {
	const txt = v.trim();
	if (txt.length < 2) {
		search.cancel();
		results.value = [];
		return;
	}
	results.value = null;
	search(txt);
});

watch(searchOpen, (open) => open && nextTick(() => searchInput.value && searchInput.value.focus()));

async function afterLink() {
	await Promise.all([load(), refreshConversation()]);
}

async function link(partyType, party) {
	busy.value = true;
	try {
		await api.setParty(store.selected, partyType, party);
		frappe.show_alert({ message: __("Linked to {0}", [frappe.utils.escape_html(party)]), indicator: "green" });
		await afterLink();
	} catch (e) {
		// Frappe showed the error
	} finally {
		busy.value = false;
	}
}

function createLead() {
	frappe.confirm(__("Create a Lead from this conversation's contact?"), async () => {
		busy.value = true;
		try {
			const lead = await api.createLead(store.selected);
			frappe.show_alert({ message: __("Lead {0} created", [frappe.utils.escape_html(lead)]), indicator: "green" });
			await afterLink();
		} catch (e) {
			// Frappe showed the error
		} finally {
			busy.value = false;
		}
	});
}

function openLead() {
	frappe.set_route("Form", "Lead", card.value.party);
}

function newDoc(doctype, values) {
	frappe.new_doc(doctype, values);
}
</script>

<style scoped>
.db-card-pane {
	display: flex;
	flex-direction: column;
	height: 100%;
	min-height: 0;
}
.db-card-head {
	display: flex;
	justify-content: space-between;
	align-items: center;
	padding: 8px 12px;
	border-bottom: 1px solid var(--border-color);
	background: var(--fg-color);
	min-height: 41px;
}
.db-card-title {
	font-weight: 600;
}
.db-card-body {
	flex: 1 1 auto;
	overflow-y: auto;
	min-height: 0;
}
.db-pad {
	padding: 12px;
}
.db-section {
	padding: 10px 12px;
	border-bottom: 1px solid var(--border-color);
	display: flex;
	flex-direction: column;
	gap: 4px;
}
.db-section-title {
	font-size: var(--text-xs, 12px);
	font-weight: 600;
	text-transform: uppercase;
	letter-spacing: 0.04em;
	color: var(--text-muted);
}
.db-sub {
	margin-top: 6px;
	font-size: var(--text-xs, 12px);
	font-weight: 600;
	color: var(--text-muted);
}
.db-contact-name,
.db-party-name {
	font-weight: 600;
	overflow-wrap: anywhere;
}
.db-party-name {
	color: var(--text-color);
}
.db-line {
	font-size: var(--text-sm, 13px);
	overflow-wrap: anywhere;
}
.db-muted {
	color: var(--text-muted);
}
.db-small {
	font-size: var(--text-xs, 12px);
}
.db-pick {
	display: flex;
	align-items: center;
	gap: 8px;
	padding: 4px 0;
}
.db-pick-main {
	flex: 1 1 auto;
	min-width: 0;
}
.db-pick-name {
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.db-actions {
	display: flex;
	flex-wrap: wrap;
	gap: 6px;
	margin-top: 6px;
}
.db-search-box {
	display: flex;
	flex-direction: column;
	gap: 4px;
	margin-top: 6px;
}
.db-stats {
	display: flex;
	gap: 12px;
	margin-top: 6px;
	padding: 8px;
	border-radius: var(--border-radius, 6px);
	background: var(--control-bg);
}
.db-stat-value {
	font-weight: 600;
}
.db-stat-totals {
	flex: 1 1 auto;
	min-width: 0;
}
.db-total {
	overflow-wrap: anywhere;
}
.db-doc {
	display: grid;
	grid-template-columns: 1fr auto;
	column-gap: 8px;
	padding: 4px 6px;
	margin: 0 -6px;
	border-radius: var(--border-radius, 6px);
	color: var(--text-color);
	text-decoration: none;
	font-size: var(--text-sm, 13px);
}
.db-doc:hover {
	background: var(--control-bg);
	text-decoration: none;
}
.db-doc-name {
	font-weight: 500;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.db-doc .db-muted {
	grid-column: 1;
}
.db-doc-amount {
	grid-column: 2;
	grid-row: 1 / span 2;
	align-self: center;
	white-space: nowrap;
}
.db-overdue {
	color: var(--red-600);
}
.db-badge {
	display: inline-block;
	padding: 0 8px;
	border-radius: 999px;
	background: var(--bg-blue);
}
</style>
