# Plan 3 (Agent tools) — follow-ups and rulings

DialogBrain side: PR saloprj/dialogbrain#5382 (branch feature/erpnext-integration-context).

## Release gates
- Live e2e (plan task B6) against a DialogBrain running the PR branch.
- One full test run on a Frappe v15 / Python 3.10 bench.
- Media Range support (from Plan 2).

## Rulings
P1: Part A tests run in the CI image, never against dialogbrain-postgres: `sudo docker run --rm -v ~/projects/dialogbrain-erpnext-ctx:/repo -w /repo/apps/api-gateway -e TESTING=true -e PYTHONPATH=/repo:/repo/libs ci-gateway-tools:f3e43fbac4d8 python -m pytest <paths> -q`; tests that need postgres/redis use throwaway containers (`docker run --rm -d --name dbctx-pg-<task> postgres:16` + schema via scripts/ci/load_test_schema.sh, and redis) on a private docker network, removed afterwards. — cost if wrong: some DB tests not run locally (CI covers).
P2: browser checks in Part B by implementer (headless chromium) or controller.
Ruling R1: REST API-key callers (api_key_id set, no mcp_session_id) get a clean early refusal in workspace.switch before membership query/audit; fix PR note; fix executor comment re concurrency; add same-workspace/different-user cache test. — cost: none.
Ruling R1a: accept ToolContext.rest_api flag (set only by REST /execute) as the guard instead of 'api_key_id && !mcp_session_id' — the latter also matches MCP no-session callers whose journal row is pinned by tests.
Ruling R2: remove both tools from public directory profiles (/mcp/chatgpt, /mcp/claude-directory) — not part of this PR's purpose; directory listing is a product decision — keep full catalog only. operator_only flag OK.
Ruling R3 (for B4): ERPNext treats paused_by == "operator" as taken over; null (human reply) also shows as "agent paused" in UI but override stays default.
Ruling R4 (from A3 review minor 1, carried to B1/B2): ERPNext enforces allow_agent_writes server-side in create_lead/create_draft_order (refuse when off) — DialogBrain denied_tools is not a boundary. B2 verifies signature on the raw request URI path and raw body bytes (frappe.request.get_data()).
Ruling R5: accept "no document permissions for DialogBrain Agent API role" (deviation from brief) — a leaked agent key must not be usable against /api/resource — cost if wrong: none (tools don't need them).
Ruling R6: a Lead created by the agent links the conversation with link_source="auto_unverified" (not manual) — the agent acted on unverified chat claims; a manager's manual link upgrades it — cost if wrong: manager must confirm the link before the agent sees that party's data.
Ruling R7: create_lead reuses the Lead already tied to this chat (linked party or db_conversation) and on duplicate email returns a generic "a lead with these details already exists" (no other Lead's name) — avoids email-existence oracle and lead spam; wrap immediate _log in try (minor 4).
Ruling R8: log a short `reason` code for unidentified contexts (no_headers | bad_signature | stale | unknown_thread | unlinked | unverified) in DB Agent Call Log.error — needed to debug secrets in B3/B6; never returned to the caller — cost: none.
Ruling R9: agent_api refuses ANY query string on all agent_api calls (not only context-bearing) → not identified / ValidationError; all args come from the JSON body. qty must be a finite number (not bool), 0 < qty ≤ 100000. Clear message_log produced during the elevated insert and return a generic error to the LLM on insert failure (details only in the call log).
Ruling R10: new Part A task A6 — integrations.add_endpoints accepts optional per-endpoint `description` (→ tool description shown to the LLM) and optional `request_schema` (JSON Schema for the body, used instead of the inferred sample shape); also /execute error responses carry `error_type`. Same branch/PR #5382 (push needs user's yes). ERPNext B3 then sends request_schema for create_draft_order.items and matches permission_denied by error_type with wording fallback. — without it the agent sees undescribed tools — cost if wrong: one more DialogBrain commit.
Ruling R11 (carried into B4): in set_agent_writes 1→0, frappe.db.commit() right after the local store (an unexpected exception must not roll back "writes off").
Ruling R12 (I1): set_party requires frappe.has_permission(party_type, "read", party) for the caller; otherwise refused. create_lead by a manager keeps manual. — cost if wrong: agents without Customer read can't link (they also can't search parties already).
Ruling R13 (I3): (a) pasted-key Connect on a connected site refuses a different workspace (same guard as OAuth). (b) DB Conversation gets `dialogbrain_instance` (Data, server-set) = sha1(normalised base_url + "|" + workspace_id)[:16], stamped on upsert, patched for existing rows from current Settings; thread_context.resolve requires it to equal the current Settings instance (else reason `other_instance`). (c) register() verifies the stored integration's base_url equals this site's public URL via integrations.get_endpoints before reuse; otherwise drops integration_id and creates a new one. (d) when instance changes (base_url/workspace differ from stored), clear integration_id, thread_context_secret, tool-name map. — cost if wrong: one extra registration; old-instance conversations never identify (safe).
Ruling R14p3 (I6): v15 bench run + B6 are release gates recorded in follow-ups, not merge blockers.
Ruling R15p3 (breaker adjudication, deviates from "no second fix wave"): both residuals are real, load-bearing and 5-line fixes on the data-leak boundary → one minimal targeted patch by the same implementer, verified inline by the controller: (a) empty instance key on either side → other_instance; (b) Settings.validate refuses changing api_key/base_url while connected ("Disconnect first"), form-save path only (server writes unaffected). — cost if wrong: admins must Disconnect before changing the key (already the intended flow).

## Deferred minor findings
- A1: minor (deferred): human_replied not directly exercised (chain pinned elsewhere; state in PR); no in-flight SET NX lock (follow-up); failures never cached → B4 must treat cache miss as possible.
- A2: minor (deferred): unscoped db.rollback on 0-rows race; brittle getsource route tests.
- A3: minor (deferred): ref resolution prefers ca.workspace over t.workspace; no rollback after failed resolve; AI-panel fallback thread steerable; underscore header spellings not stripped; put_secret/config write atomicity unchecked.
- B1: minor (deferred): agent key REST surface via All/Desk User perms (accepted residual risk; document; check v15 get_item_details); no rate limit on refused-call log rows; callback commit resets later after_rollback callbacks (comment); log error may hold PII; password-reset mail to agent address; get_item LIKE wildcards/uom/ordering; db_conversation no search_index; weak install test.
- B1: minor (deferred): duplicate response is an email-existence oracle (inherent); existing-Lead shortcut drops new note/phone.
- B2: minor (deferred): hooks/server scripts on SO insert run as Administrator (admin-configured; document); report's "form body hashes empty" claim wrong (fix docstring).
- B2: minor (deferred): _insert_elevated turns DB deadlocks into ValidationError; tiny positive qty → generic error; Content-Type unsigned (negligible); reason codes share the error column (separate context_reason field later); /api/v2/method paths → no context.
- B3: minor (deferred): _attach_agents re-enables integration on agents an operator disabled / overwrites per-agent deny lists (document in B5); agents created after Connect need Reconnect (document in B5); toggle bumps modified (TimestampMismatch with unsaved edits).
- A6: minor (deferred): /approve doesn't return error_type; existing callers sending a description key now validated.
- B3: minor (deferred): no test for "create fails → fresh key rolled back".
- A6: minor (deferred): Cf stripping removes ZWJ/ZWNJ (Arabic/Indic/emoji); U+2028/2029 not stripped; jsonschema meta-validation silently skipped if lib missing.
- B4: minor (deferred): can_retry ignores 120 s settle gap; agent_override drift (UI reads live status).
- B4: minor (deferred → final fix wave): M1 treat 2xx-non-JSON and 408/499/520-524 as non-definite (keep dedupe); test no_response reuses stored attempt after settle; M3 _store_send_state writes raw from claim-time snapshot (merge instead).
- Final: minor (deferred): widened echo window can attach sent_by to the wrong same-text row; README "only definitely failed can be retried" imprecise; before_uninstall runs real DialogBrain calls on --dry-run; non-dict JSON classified no_response; base_url change orphans conversations from agent identification (by design; README line / re-stamp tool).

## DialogBrain-side follow-ups (after PR #5382)
- SET NX in-flight marker for client-supplied request_id (concurrent duplicates both run).
- /approve should return error_type; ref resolution in signed context should prefer t.workspace_id; rollback after failed resolve.
- Consent/agents: contact_id in message events; group sender names; consent Cancel redirect (from Plans 1b/2).
