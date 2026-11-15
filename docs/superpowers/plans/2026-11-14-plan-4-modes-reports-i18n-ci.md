# Plan 4 — Agent modes per channel, reports, translations, v15/v16 CI, Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish v1 of the app: a manager switches the AI agent per channel (copilot / autopilot / off) from ERPNext, sees three reports on the team's chat work, reads the whole UI in Ukrainian or Russian, and the app is proven on Frappe v15 as well as v16 on every push.

**Architecture:** Everything is in this app; DialogBrain needs no change. A channel's mode is the state of one DialogBrain agent trigger, read with `agents.list` + `agents.get` and changed with `agents.trigger_update`; ERPNext stores which trigger it bound and never edits a trigger other channels share. Reports are Frappe Script Reports over the mirrored `DB Conversation` / `DB Message` tables. Translations are CSV files with a completeness test. CI is a GitHub Actions matrix, and the same v15 run is available locally from a throwaway Docker bench.

**Tech Stack:** Frappe/ERPNext v15 + v16 app `dialogbrain` (Python 3.10 syntax, Vue 3 inbox), GitHub Actions, Docker.

**Spec:** `docs/superpowers/specs/2026-11-13-dialogbrain-erpnext-inbox-design.md`. Read §2 goals 5, 7, 9; §7.6 (agent modes); §7.7 (UI); §7.8 (reports); §7.10 (quality). Follow-ups that feed this plan: `docs/superpowers/plans/2026-11-13-plan-2-followups.md` (media Range, v15 run) and `…-plan-3-followups.md` (dev stand notes, browser check).

**Branch:** `feat/plan-4-modes-reports`, from `feat/plan-3-agent-tools`. PR #5 stacked on PR #4.

## Global Constraints

- Python 3.10 syntax, no v16-only Frappe API. Aggregates in `frappe.get_list` go through `dialogbrain/compat.py`; reports use parameterised `frappe.db.sql` (values never formatted into SQL; identifiers only from fixed lists in code).
- Every user-visible string: Python `_()`, JS/Vue `__()`. Source language is English.
- Tests: `sudo docker exec erpnext-backend-1 bench --site test.localhost run-tests --app dialogbrain` must stay green (590 tests at the start of this plan). New code is written test-first.
- DialogBrain is never called in tests: use the existing fakes (`FakeDialogBrain` in `tests/test_agent_integration.py`, `_mock_client` in `tests/test_send.py`).
- Never print, log or commit the key in `dev/dialogbrain-dev.env`. Never run `docker compose up` without naming services; never touch `dialogbrain-postgres` or `erpnext-db-1`.
- Commit after every task. Attribution line: `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`.
- Out of scope (stays in follow-ups): fingerprint-based reconcile probing (Plan 1 R16), a nonce for the signed context, idempotent `create_draft_order`.

## Review Focus

1. **A trigger that serves several channels.** A trigger with no `channel_account_ids`, or with more than one, answers other channels too. Switching "this channel" to off must not silence the others: such a trigger is shown, never changed. Test in Task 1.
2. **DialogBrain refuses or is down while the mode is changed.** ERPNext must keep showing the old mode, not the one the manager clicked. Test in Task 2.
3. **A report asked for by someone who may not see all channels.** A Chat Agent must not read other channels' conversations through a report. Reports are for Chat Manager and System Manager only; test in Task 4.
4. **A translation that breaks a placeholder.** `{0}` missing or renamed in a uk/ru string raises at render time for that language only. Test in Task 6.
5. **A seek request on a voice message or video.** `Range: bytes=…` must return 206 with the right `Content-Range`, and an unsatisfiable or malformed range must not become a 500 or an unbounded download. Test in Task 3.

---

### Task 1: Read a channel's agent trigger from DialogBrain

**Files:**
- Create: `dialogbrain/setup/agent_modes.py`
- Modify: `dialogbrain/dialogbrain/doctype/db_channel/db_channel.json` (new fields), `dialogbrain/setup/connect.py` (call after channels are upserted), `dialogbrain/hooks.py` (hourly job)
- Test: `dialogbrain/tests/test_agent_modes.py`

**Interfaces:**
- Consumes: `DialogBrainClient.execute("agents.list", {})` → `{"agents": [{"id", "name", "status", "send_mode", "trigger_count", "is_template"}]}` (the list also holds system templates: `is_template: true`, always `active`); `execute("agents.get", {"agent_id": id})` → agent dict with `"send_mode"` (or `execution_config.default_send_mode`) and `"triggers": [{"id", "trigger_type", "conditions", "send_mode", "enabled", "priority"}]`. `conditions.channel_account_ids` is a list of account ids as strings or ints.
- Produces:
  - DB Channel fields (all read-only in the form, set by server code only): `agent_id` (Data), `agent_name` (Data), `agent_trigger_id` (Data), `agent_mode_status` (Small Text), `agent_mode_synced_at` (Datetime). `agent_mode` becomes read-only too; its Select options become an empty first line followed by `copilot`, `autopilot`, `off` (empty = not linked), default empty.
  - `agent_modes.candidates(client) -> dict[str, list[dict]]`: per channel account id, the triggers that serve it: `{"agent_id", "agent_name", "trigger_id", "mode", "exclusive"}`.
  - `agent_modes.mode_of(trigger: dict, agent: dict) -> str`: `"off"` when `enabled` is false; else `"autopilot"` when the trigger's `send_mode` (or, when it is null, the agent's) is `"auto"`; else `"copilot"`.
  - `agent_modes.sync(client=None) -> dict`: binds every DB Channel and returns `{"linked": n, "ambiguous": n, "none": n}`.
  - `agent_modes.hourly() -> None`: scheduler entry, added as an `"hourly"` key to the existing `scheduler_events` dict in `hooks.py` (no second dict); calls `sync()` and swallows and logs any error — a site that is not connected is just such an error (`DialogBrainClient.from_settings` raises without base URL or key), no separate check (a mode changed in DialogBrain shows up in ERPNext within the hour without a click).

Rules `sync` applies per channel, in this order:
1. Only `incoming_message` triggers of agents whose `status` is `active` and that are not templates (`is_template` true is skipped, and `agents.get` is not called for it) count. A trigger serves the channel when the channel's `account_id` is in `conditions.channel_account_ids` (compare as strings). `exclusive` is true when that list has exactly this one id.
2. A channel that already has `agent_trigger_id` keeps it while that trigger is still among its exclusive candidates; its `agent_mode` is refreshed from DialogBrain. A bound trigger that was deleted, or that stopped being exclusive (a second account was added to it in DialogBrain), is unbound and the channel falls through to rules 3–6.
3. Otherwise, exactly one exclusive candidate → bind it and set `agent_mode` from `mode_of`.
4. Several exclusive candidates → `agent_mode = ""`, status `Several agents answer this channel: choose one` (the choice is Task 2).
5. No exclusive candidate but shared ones → `agent_mode = ""`, status `The agent trigger also serves other channels: change it in DialogBrain`.
6. None → `agent_mode = ""`, status `No agent answers this channel in DialogBrain`.

A DialogBrain error in `sync` is caught by the caller in `connect.py`: Connect never fails because of it; the text goes to every channel's `agent_mode_status` only when nothing could be read at all, and bindings and modes already stored are left as they are. DialogBrain gates these tools by the role of the user who owns the API key (`agents.list`/`agents.get` need member or above, `agents.trigger_update` needs edit rights): a viewer's key is answered `permission_denied`, and the status then reads `The DialogBrain key may not read agents: connect with a member, admin or owner key`.

`autopilot` means "the trigger lets the agent answer by itself". DialogBrain's safety gate can still turn a single answer into a draft; ERPNext does not see that. The README says so (Task 7).

- [ ] **Step 1: Write the failing tests** in `tests/test_agent_modes.py` with a fake client returning two agents and these triggers; assert the stored fields:

```python
# agent 10 "Sales" (active, send_mode "draft"): trigger 1 {channel_account_ids: ["101"], send_mode: None, enabled: True}
# agent 10: trigger 2 {channel_account_ids: ["102"], send_mode: "auto", enabled: True}
# agent 10: trigger 3 {channel_account_ids: ["103"], send_mode: "auto", enabled: False}
# agent 11 "Support" (active): trigger 4 {channel_account_ids: [104], send_mode: "draft", enabled: True}
# agent 10: trigger 5 {channel_account_ids: ["104"], send_mode: "auto", enabled: True}
# agent 11: trigger 6 {channel_account_ids: ["105", "106"], send_mode: "auto", enabled: True}
# agent 11: trigger 7 {conditions: {} , send_mode: "auto", enabled: True}        # no account filter
# agent 12 "Old" (paused): trigger 8 {channel_account_ids: ["107"], enabled: True}
def test_modes_are_read_from_the_exclusive_trigger(self):
    # 101 -> copilot (inherits the agent's draft), trigger 1; 102 -> autopilot; 103 -> off
def test_two_exclusive_agents_on_one_channel_are_not_guessed(self):      # 104 -> "", "Several agents…"
def test_a_shared_trigger_is_shown_and_not_bound(self):                  # 105 -> "", "…also serves other channels…", agent_trigger_id empty
def test_a_channel_nobody_answers(self):                                 # 107 (paused agent) and 108 -> "", "No agent answers…"
def test_a_bound_trigger_survives_a_second_agent_appearing(self):        # bind 104 to trigger 4, sync again -> still 4
def test_a_bound_trigger_that_is_gone_is_unbound(self):                  # trigger deleted in DialogBrain
def test_a_bound_trigger_that_became_shared_is_unbound(self):            # bound trigger 1 now lists ["101", "109"] and no other trigger lists only "101" (assert that in the test) -> 101: "", "…also serves other channels…"
def test_system_templates_are_ignored(self):                             # agent 13 is_template, trigger on "108" -> 108 "No agent answers…"; agents.get never called with 13
def test_a_key_that_may_not_read_agents(self):                           # agents.list -> permission_denied: connect() returns normally, status names the key role, stored bindings untouched
def test_hourly_swallows_errors_and_skips_a_disconnected_site(self):
def test_connect_survives_agents_list_failing(self):                     # connect() returns normally
def test_account_ids_compare_as_strings(self):                           # 104 given as int
```

- [ ] **Step 2: Run** `bench --site test.localhost run-tests --app dialogbrain --module dialogbrain.tests.test_agent_modes` — expected: FAIL (module missing).
- [ ] **Step 3: Implement** the fields (then `bench --site test.localhost migrate`) and `agent_modes.py`. Add a patch `dialogbrain/patches/v0_1/clear_unlinked_agent_mode.py` that sets `agent_mode = ""` on channels without `agent_trigger_id` (the old default `copilot` claimed a mode nothing backed), listed in `patches.txt`.
- [ ] **Step 4: Run** the module, then the whole app suite — expected: all pass.
- [ ] **Step 5: Commit** `feat(modes): read each channel's agent trigger from DialogBrain`.

### Task 2: Change the mode from ERPNext

**Files:**
- Create: `dialogbrain/api/channels.py`, `dialogbrain/dialogbrain/doctype/db_channel/db_channel.js`
- Modify: `dialogbrain/public/js/inbox/ConversationHeader.vue` (mode chip; `get_conversations` already returns the channel's `agent_mode`, so `api/conversations.py` does not change)
- Test: `dialogbrain/tests/test_agent_modes.py`

**Interfaces:**
- Consumes: Task 1; `execute("agents.trigger_update", {"trigger_id": int, "agent_id": int, "send_mode": "auto"|"draft", "enabled": bool})` — DialogBrain requires both ids and looks the trigger up by trigger, agent and workspace; `agent_id` is the one stored on the channel in Task 1.
- Produces (all `@frappe.whitelist(methods=["POST"])`, allowed for Chat Manager and System Manager, refused for Chat Agent with `frappe.PermissionError`):
  - `dialogbrain.api.channels.agent_options(channel) -> {"mode", "status", "agent_id", "agent_name", "trigger_id", "choices": [{"trigger_id", "agent_name", "mode", "exclusive"}]}` — live from DialogBrain.
  - `dialogbrain.api.channels.choose_agent(channel, trigger_id) -> same dict` — only an exclusive candidate of that channel may be chosen.
  - `dialogbrain.api.channels.set_agent_mode(channel, mode) -> same dict` — `mode` one of `copilot`, `autopilot`, `off`.
  - `dialogbrain.api.channels.refresh_agent_modes() -> {"linked", "ambiguous", "none"}`.

`set_agent_mode` sends, always with the channel's `trigger_id` and `agent_id`: `off` → `{"enabled": false}`; `copilot` → `{"enabled": true, "send_mode": "draft"}`; `autopilot` → `{"enabled": true, "send_mode": "auto"}`. It writes `agent_mode` and `agent_mode_synced_at` only after DialogBrain answered success; on `DialogBrainError` nothing is stored and the user gets `DialogBrain did not change the agent mode: {0}` (the key scrubbed, as `_agent_reason` does in `api/conversations.py`). Without a bound trigger it throws `This channel has no agent to switch. Open the channel and choose one.`

Form script `db_channel.js`: a "DialogBrain agent" section showing agent name, mode and status; buttons **Copilot**, **Autopilot**, **Off** (the current one highlighted), **Choose agent…** (a dialog listing `choices`, only when there are several exclusive ones), **Refresh from DialogBrain**. Inbox header: a small chip next to the channel name: `AI: drafts` / `AI: answers` / `AI: off`, nothing when the mode is empty.

- [ ] **Step 1: Write the failing tests:**

```python
def test_set_mode_sends_the_trigger_update_and_stores_the_mode(self):
    # copilot -> {"trigger_id": 2, "agent_id": 10, "enabled": True, "send_mode": "draft"}; DB Channel.agent_mode == "copilot"
def test_off_disables_the_trigger_and_autopilot_enables_it_again(self):
def test_dialogbrain_refusing_keeps_the_old_mode(self):          # fake.fail["agents.trigger_update"]; mode unchanged; message has no key
def test_no_bound_trigger_is_refused_without_calling_dialogbrain(self):
def test_a_shared_trigger_can_not_be_chosen(self):
def test_choose_agent_binds_and_reads_its_mode(self):
def test_chat_agent_is_refused(self):                            # PermissionError on all four methods
def test_unknown_mode_is_refused(self):
def test_a_key_without_edit_rights_keeps_the_old_mode(self):    # trigger_update -> permission_denied; message names the key role
```

- [ ] **Step 2: Run** the module — expected: FAIL.
- [ ] **Step 3: Implement** `api/channels.py`, the form script (register in the doctype folder; no `hooks.py` change is needed for a doctype's own `.js`), and the header chip.
- [ ] **Step 4: Run** the app suite and `bench build --app dialogbrain` — expected: pass, build without errors.
- [ ] **Step 5: Commit** `feat(modes): switch a channel between copilot, autopilot and off`.

### Task 3: Seek in voice messages and video (HTTP Range in the media proxy)

**Files:**
- Modify: `dialogbrain/api/media.py`
- Test: `dialogbrain/tests/test_media.py`

**Interfaces:**
- Consumes: the existing `get(message, file_id)` and `_open_upstream(file_id, numeric_id)`.
- Produces: `get` honours one `Range: bytes=a-b | a- | -n` header by passing it to the upstream storage request and relaying its answer.

Behaviour:
- Every response of an inline audio/video type carries `Accept-Ranges: bytes`.
- A syntactically valid single range is forwarded upstream unchanged. Upstream 206 → respond 206 with upstream's `Content-Range` and `Content-Length`. Upstream 200 (storage ignored the range) → respond 200 with the full body, as today.
- Upstream 416 → respond 416 with `Content-Range: bytes */<size>` when upstream gave it, empty body.
- A malformed header or a multi-range (`bytes=0-1,5-6`) is ignored: full 200 response.
- All existing headers stay (nosniff, the sandbox CSP, the inline allow-list, `Content-Disposition`); the size limit applies to what is actually streamed.

- [ ] **Step 1: Write the failing tests** (mocked upstream response objects, as the file's existing tests do):

```python
def test_range_request_is_forwarded_and_answered_206(self):       # Range: bytes=100-199 -> 206, Content-Range "bytes 100-199/5000", body 100 bytes
def test_open_ended_and_suffix_ranges_are_forwarded(self):        # "bytes=100-", "bytes=-500"
def test_storage_that_ignores_range_gives_a_full_200(self):
def test_unsatisfiable_range_is_416_not_500(self):
def test_malformed_or_multi_range_is_ignored(self):               # "bytes=abc", "items=0-1", "bytes=0-1,5-6" -> 200, no Range sent upstream
def test_audio_and_video_advertise_accept_ranges(self):
def test_range_keeps_the_security_headers(self):
```

- [ ] **Step 2: Run** `--module dialogbrain.tests.test_media` — expected: FAIL.
- [ ] **Step 3: Implement.** Parse with one regex `^bytes=(\d*)-(\d*)$` (at least one side non-empty); pass `headers={"Range": value}` to the upstream GET.
- [ ] **Step 4: Run** the module and the app suite — expected: pass.
- [ ] **Step 5: Commit** `feat(media): seek in voice and video (Range requests)`.

### Task 4: Reports

**Files:**
- Create: `dialogbrain/dialogbrain/report/first_response_time/{__init__.py,first_response_time.json,first_response_time.py,first_response_time.js}`, `…/report/unanswered_conversations/…`, `…/report/conversations_by_channel_and_manager/…`, `dialogbrain/dialogbrain/report/__init__.py`
- Modify: `dialogbrain/dialogbrain/workspace/dialogbrain/dialogbrain.json` (three report shortcuts)
- Test: `dialogbrain/tests/test_reports.py`

**Interfaces:**
- Produces three Script Reports (`is_standard: Yes`, `module: DialogBrain`, `report_type: Script Report` — without these two `migrate` does not create the Report documents —, `ref_doctype: DB Conversation`, roles **Chat Manager** and **System Manager** only), each with `execute(filters=None) -> (columns, data)`.

**First Response Time** — filters: `from_date`, `to_date` (default: last 30 days), `group_by` (`Manager` | `Channel` | `Day`, default `Manager`), `channel` (Link DB Channel, optional), `assigned_to` (Link User, optional). Base set: conversations with `opened_at` in the period. Columns: group, `Conversations` (all), `Answered` (`first_response_seconds > 0`), `Not answered yet`, `Average (min)`, `Median (min)`, `Longest (min)`, `Within 5 min (%)`. Minutes rounded to 1 decimal; the median is computed in Python from the per-group lists. Group `Manager` uses `assigned_to` (empty → "Unassigned"); `Day` uses the date of `opened_at`.

**Unanswered Conversations** — filters: `channel`, `assigned_to`, `min_waiting_minutes` (Int, default 0). Rows: conversations with `status = "Open"` and `last_message_direction` = `in` (the value `sync/upsert.py` writes for inbound), ordered by `last_inbound_at` ascending. Columns: conversation (Link), title, channel, assigned to, last inbound at, `Waiting (min)`, unread, last message preview.

**Conversations by Channel and Manager** — filters: `from_date`, `to_date`, `group_by` (`Channel` | `Manager`). Columns: group, `Opened` (by `opened_at`), `Closed` (by `closed_at`), `Open now`, `Unanswered now`, `Inbound messages`, `Outbound messages`, `By AI` (outbound with `author_type = "agent"`). Message counts come from `DB Message.sent_at` in the period joined to its conversation.

- [ ] **Step 1: Write the failing tests** on rows created in the test (`_conv(...)` helpers as in `tests/test_inbox_api.py`), with fixed timestamps:

```python
def test_first_response_averages_median_and_share_by_manager(self):
    # manager A: 60 s, 120 s, 600 s, one unanswered -> Conversations 4, Answered 3, Average 4.3, Median 2.0, Longest 10.0, Within 5 min 66.7
def test_first_response_by_channel_and_by_day(self):
def test_first_response_respects_the_period_and_filters(self):
def test_unanswered_lists_open_chats_waiting_for_us_oldest_first(self):   # closed and answered ones absent
def test_unanswered_min_waiting_minutes(self):
def test_by_channel_counts_opened_closed_and_messages(self):
def test_empty_period_returns_no_rows_not_an_error(self):
def test_chat_agent_can_not_run_the_reports(self):
    # frappe.set_user(agent); frappe.get_doc("Report", name).is_permitted() is False for all three
def test_report_sql_takes_values_as_parameters(self):                      # a channel filter of "x' OR '1'='1" returns no rows
```

- [ ] **Step 2: Run** `--module dialogbrain.tests.test_reports` — expected: FAIL.
- [ ] **Step 3: Implement** the three reports and their `.js` filter definitions; add the shortcuts; `bench --site test.localhost migrate`.
- [ ] **Step 4: Run** the app suite — expected: pass. Open each report once on the `frontend` site (`bench --site frontend execute frappe.desk.query_report.run --kwargs '{"report_name": "First Response Time"}'`) — expected: columns and rows, no traceback.
- [ ] **Step 5: Commit** `feat(reports): first response time, unanswered conversations, conversations by channel and manager`.

### Task 5: Proof on Frappe v15 — local run and CI matrix

**Files:**
- Create: `.github/workflows/ci.yml`, `dev/test-v15.sh`
- Modify: whatever v15 turns up (expected places: `dialogbrain/compat.py`, `sanitize_html` use in `sync/upsert.py`, `frappe.db.after_rollback` use in `agent_api.py`, the desk bundle's navbar badge), `README.md` (supported versions, how to run both)
- Test: the whole suite, on both versions

**Interfaces:**
- Produces:
  - `dev/test-v15.sh`: runs the app's suite on Frappe v15 / ERPNext v15 / Python 3.10 in throwaway containers on its own Docker network (`dbx-v15-<pid>`): `mariadb:10.6`, `redis:7`, and `frappe/erpnext:v15` with this repo mounted read-only and copied into `apps/dialogbrain`. It creates a site, installs `erpnext` and `dialogbrain`, runs `bench build --app dialogbrain` and `bench run-tests --app dialogbrain`, prints the last lines, and removes the containers and the network on exit (trap). It never uses the `erpnext` compose project or its volumes. The `frappe/erpnext:v15` image has no compiler: `pip install -e apps/dialogbrain` works only while the app has no dependency that needs building; the script installs it with `pip install --only-binary=:all: -e apps/dialogbrain`, so pip itself refuses a source build with a clear error, and a comment in the script explains the flag.
  - `.github/workflows/ci.yml`: on push and pull_request; matrix `{frappe: version-15, python: "3.10", node: 18, mariadb: "10.6"}` and `{frappe: version-16, python: "3.14", node: 24, mariadb: "11.8"}`; services mariadb + redis; steps: `pip install frappe-bench`, `bench init --frappe-branch <branch> --skip-redis-config-generation`, `bench get-app erpnext --branch <branch>`, `bench get-app dialogbrain $GITHUB_WORKSPACE`, `bench new-site`, `bench --site … install-app erpnext dialogbrain`, `bench build --app dialogbrain`, `bench --site … set-config allow_tests true`, `bench --site … run-tests --app dialogbrain`. Before writing the versions, read the `version-16` branch's `pyproject.toml` and `package.json` (`requires-python`, `engines.node`) and use what they say.

- [ ] **Step 1:** Write `dev/test-v15.sh` and run it. Expected first result: failures. Save the failing test names to the task report.
- [ ] **Step 2:** For each failure, fix the app so the same code passes on both versions (version checks only through `dialogbrain/compat.py`). Re-run `dev/test-v15.sh` until it ends with `OK`, then the v16 suite on `test.localhost` — both green.
- [ ] **Step 3:** Open the inbox once on the v15 container (`bench --site … execute dialogbrain.api.conversations.get_conversations`) and confirm the bundle built (`sites/assets/dialogbrain/dist/js/*.bundle.*.js` exists).
- [ ] **Step 4:** Write `.github/workflows/ci.yml`. It is first run by GitHub when the branch is pushed (the push needs the user's go-ahead); a red run there is fixed in this task. Frappe v16 needs Python `>=3.14,<3.15`: a v16 job that dies in `bench init`/pip (a dependency without a 3.14 wheel) before our tests start is an infrastructure failure, recorded as such in the task report and not "fixed" in the app.
- [ ] **Step 5: Commit** `ci: test on Frappe v15 and v16; fix what v15 turned up`.

### Task 6: Ukrainian and Russian translations

**Files:**
- Create: `dialogbrain/translations/uk.csv`, `dialogbrain/translations/ru.csv`, `dialogbrain/tests/test_translations.py`
- Modify: source files where a visible string is not yet wrapped in `_()` / `__()`

**Interfaces:**
- Produces: CSV rows `source,translation,context` (context empty), UTF-8, one row per distinct source string, for: every `_("…")` in `dialogbrain/**/*.py`, every `__("…")` in `dialogbrain/public/js/**` and the doctype/report `.js` files, and the labels of this app's DocTypes, their fields, Select options shown to users, reports, report columns and filters, the page title and the workspace.

Glossary (use exactly, in every string):

| English | Українська | Русский |
|---|---|---|
| Chats | Чати | Чаты |
| Conversation | Розмова | Диалог |
| Take over | Перехопити | Перехватить |
| Give back to agent | Повернути агенту | Вернуть агенту |
| Assign | Призначити | Назначить |
| Unassigned | Не призначено | Не назначено |
| Unanswered | Без відповіді | Без ответа |
| Draft | Чернетка | Черновик |
| Quick reply | Швидка відповідь | Быстрый ответ |
| Note | Нотатка | Заметка |
| Copilot / Autopilot / Off | Підказки / Автовідповідь / Вимкнено | Подсказки / Автоответ / Выключено |
| Customer / Lead | Клієнт / Лід | Клиент / Лид |
| Connect / Reconnect / Disconnect | Підключити / Перепідключити / Відключити | Подключить / Переподключить / Отключить |

Rules: product names stay as they are (DialogBrain, ERPNext); the tone is plain and polite, no English left in a sentence; every `{0}`-style placeholder of the source appears in the translation exactly once.

- [ ] **Step 1: Write the failing tests:**

```python
def test_every_visible_string_has_uk_and_ru(self):
    # regexes over the source tree collect _("…") / __("…") literals (single and double quotes, no f-strings);
    # plus labels from the app's doctype/report/page/workspace JSON; each must be a key in both CSVs
def test_placeholders_survive_translation(self):         # the multiset of {n} tokens equals the source's, per row, per language
def test_no_empty_or_untranslated_rows(self):            # translation non-empty and != source, except an allow-list (e.g. "DialogBrain", "ID", "URL")
def test_csv_has_no_duplicate_sources(self):
def test_frappe_loads_the_translations(self):            # frappe.translate.clear_cache() first (translations are cached in Redis); frappe._("Take over", lang="uk") == "Перехопити"; lang="ru" == "Перехватить"
def test_no_visible_literal_is_left_unwrapped_in_vue(self):
    # template text nodes and title=/placeholder= attributes with Latin letters must go through __();
    # {{ … }} expressions are stripped before the check, and an allow-list in the test (product names
    # "DialogBrain", "ERPNext", "AI", single symbols and file-type labels) is exempt — keep it short and explicit
```

- [ ] **Step 2: Run** `--module dialogbrain.tests.test_translations` — expected: FAIL.
- [ ] **Step 3:** Wrap what the last test finds; write both CSVs. Frappe v15 and v16 both read `translations/<lang>.csv` (`frappe/translate.py`), so no PO files are made.
- [ ] **Step 4: Run** the app suite on v16 and `dev/test-v15.sh` — expected: both pass. Switch the dev user's language to Ukrainian on the `frontend` site and open `/app/dialogbrain-inbox`: no English in the inbox.
- [ ] **Step 5: Commit** `feat(i18n): Ukrainian and Russian translations`.

### Task 7: Dev stand, README, and the checks that were still owed

**Files:**
- Modify: `dev/compose.dialogbrain.yml`, `dev/install.sh`, `README.md`, `docs/superpowers/plans/2026-11-13-plan-3-followups.md` (mark what this plan closed)

**Interfaces:**
- Produces:
  - Dev stand: the `frontend` service joins `dialogbrain_dialogbrain-network` with the alias `erpnext-dev`; `dev/install.sh` prints that DialogBrain's `SSRF_ALLOW_HTTP_HOSTS` must be set to `minio,erpnext-dev` (the variable replaces the compose default `minio`; leaving `minio` out breaks DialogBrain's media proxy) followed by a restart of `api-gateway`, and that the site's Public site URL for the dev stand is `http://erpnext-dev:8080` (why: `host.docker.internal` is 10.200.0.1 here and DialogBrain refuses 10/8 for integration calls). The script changes nothing in the DialogBrain stack.
  - README: sections "AI agent mode per channel" (what each mode does; autopilot = the agent may answer by itself, DialogBrain's safety gate can still hold an answer as a draft; the shared-trigger rule; who may switch; the Connect key must belong to a member, admin or owner of the workspace; modes refresh hourly and on "Refresh from DialogBrain"), "Reports", "Languages", "Supported versions and tests" (v15 + v16, `dev/test-v15.sh`, CI).

- [ ] **Step 1:** Change the compose file and `install.sh`; apply with `sudo docker compose -p erpnext -f pwd.yml -f dialogbrain_erpnext/dev/compose.dialogbrain.yml up -d --no-deps frontend` from `/home/denys/projects/erpnext`; verify `sudo docker inspect erpnext-frontend-1` lists the alias.
- [ ] **Step 2:** Write the README sections.
- [ ] **Step 3: Browser check** on `http://localhost:8080/app/dialogbrain-inbox` (the user's own signed-in session, or the test user whose password is in the project's scratch notes): the header chip; on a DB Channel form the three mode buttons and "Refresh from DialogBrain"; each report opens from the workspace; a voice message seeks; the UI in Ukrainian. Take over / Give back needs DialogBrain with PR #5382: check it if the dev gateway runs that code, otherwise record it as still owed. Record what was seen in the task report.
- [ ] **Step 4:** Run the app suite and `dev/test-v15.sh` one last time — expected: both `OK`.
- [ ] **Step 5: Commit** `docs: agent modes, reports, languages, versions; dev stand alias`.

---

## Self-review notes

- Spec coverage: §7.6 per-channel mode → Tasks 1–2 (per-conversation take over was Plan 3); §7.8 reports and workspace shortcuts → Task 4; goal 9 (v15 + v16, uk/ru/en) → Tasks 5–6; §7.10 CI → Task 5; Plan 2 R11 (Range) → Task 3; Plan 3 dev-stand note and owed browser check → Task 7.
- "Settings shows the mapping and the user picks the agent" (§7.6) is implemented on the DB Channel form rather than in Settings: the choice is per channel, and that is where the mode buttons are. My call.
- Managers in the First Response Time report are the assignee, not the person who typed the first reply: the mirror stores the first-response time on the conversation, not its author. My call; a per-author report needs a new column and is left out.
