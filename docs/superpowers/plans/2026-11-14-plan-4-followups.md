# Plan 4 (agent modes, reports, translations, v15/v16 CI) — follow-ups and rulings

Final state: v16 657 tests + 14 translation tests OK; v15 (`dev/test-v15.sh`, Python 3.10) 671 tests OK.

## Still owed
- First GitHub Actions run of `.github/workflows/ci.yml` (nothing was pushed when the plan finished). Watch for a missing `bench setup requirements --dev`.
- Browser: the mode chip in the conversation header (the only bound channel on the dev stand has no conversations); Take over / Give back (needs a DialogBrain gateway with PR #5382); the navbar badge on v15.
- Check once on the bench how the glossary overrides (Customer, Lead, Draft, Note, Assign, Off, Connect, Conversation) read on ERPNext's own screens in uk/ru.

## Rulings
- P1: the CI workflow is written and validated locally; its first run is after the push.
- P2: the browser check was done by the controller on the dev site (2026-11-14): channel form headline and menu, "Refresh from DialogBrain", the three reports, Range answers (206) on two real videos, the Ukrainian UI as the e2e test user. Modes were not switched (real dev triggers).
- R1p4: the app's translations must not override Frappe/ERPNext wording. UI strings that collide carry the context "DialogBrain"; DocType labels and Select options take Frappe's wording; glossary terms are the only allowed overrides (`GLOSSARY_OVERRIDES` in `tests/test_translations.py`, enforced by `test_no_translation_overrides_another_app`). Deviates from the plan's "context empty".
- R2p4: after the fix wave's re-review one targeted patch was allowed (commit 98c531d) instead of parking a wrong message to managers.
- R3p4: accepted `off` as a glossary override, channel types sharing a `_` prefix treated as related (errs toward a warning), the catch-all fixture trigger limited to email.

## Deferred findings
- `agent_mode_status` is stored already translated (language of whoever triggered the write); store the English source and translate at display.
- A ranged media request is not checked against the size cap by total size (only the partial length).
- First Response Time: a reply in the same second (0 s) counts as not answered.
- CI: add `permissions: contents: read`; `on: [push, pull_request]` runs twice per PR push.
- `last_message_preview` still uses Frappe's `strip_html` and can lose text like `<john@example.com>`; `_plain()` leaves `<o:p>`, `<!DOCTYPE>`, `<?xml ?>` visible.
- Refresh counts: a channel served only by a catch-all trigger now counts as `ambiguous` (not in README).
- When one `agents.get` fails, unbound channels are decided without that agent's triggers for that run (may bind or miss the warning until the next sync).
- `set_agent_mode` re-reads every agent to switch one channel (slow with many agents); a Chat Manager can still switch modes after Disconnect (the key is kept).
- `agent_options` writes behind a read-like name; `_reason` duplicates `_agent_reason`; no `opened_at` index for reports; by-channel report hides groups with only current-state counts.
- uk «Створено о» for "Created At" (Frappe v16 wording) reads oddly as a column label; bound `:title="'…'"` literals are not covered by the Vue literal test.
- Some commits carry Sonnet/Opus co-author trailers (written by subagents).
