# Plan 2 (Inbox UI) — follow-ups and rulings

## Rulings
P1: UI browser checks (T5–T8) are run by the controller in Chrome when the implementer has no browser; implementers build and report. — cost if wrong: extra controller time.
Ruling R1 (T1 concern 2): manual reopen also resets first_response_seconds to 0 (same as inbound reopen; spec: first response is per opening) — cost if wrong: one metric line.
Ruling R2 (T1 concern 3): mark_read publishes only when unread_count was > 0 — avoids an event to every user on each open — cost if wrong: none (nothing changed to report).
Ruling R3: sanitise with disallowed_tags=[form,input,button,select,textarea,option,link,meta,html,head,body,iframe,object,embed,style,base] AND drop `style` attributes server-side; T6 must also contain message HTML in its bubble (overflow:hidden; contain: paint). Remote img stays (tracking pixel accepted for v1; noted). — cost if wrong: some formatted emails lose styling.
Ruling R4 (T1 minor #1 promoted): assign uses a conditional UPDATE / for_update so two agents can't both take an unassigned conversation — it's the agent rule itself — cost: none.
Ruling R5: approve/reject return result dicts instead of throwing for terminal DialogBrain outcomes: {"status": "superseded"|"handled"|"unknown"|...,"message"}; expired/not_found/already-processed → local draft superseded (persisted, publish draft); timeout → status "unknown", draft left pending, message "outcome unknown — check the conversation before trying again"; empty/whitespace edited text refused; approved_not_sent → returns sent:false + safe send_error message. — cost if wrong: UI shows a status instead of an error toast.
Ruling R6: inline only for an explicit allowlist (image/jpeg,png,gif,webp,bmp,avif; audio/mpeg,ogg,wav,webm,aac,mp4,x-m4a; video/mp4,webm,ogg,quicktime); everything else attachment; always nosniff + CSP "sandbox; default-src 'none'". Promote minors #5 (UTF-8 filename* for Cyrillic) and #8 (non-dict files.info → 502) and #1 (remove ineffective call_on_close or make it effective) — cheap, user-facing.
Dev env ruling D1: for live media in dev, erpnext backend/workers join the DialogBrain docker network (compose override, external network) so presigned `minio` URLs resolve — done in T8.
Ruling R7: stats return per-currency totals: `totals: [{currency, total, count}]` plus `orders_count`; drop scalar orders_total/currency (UI shows one line per currency). Promote minor: test second-customer isolation. — cost if wrong: UI shows several lines.
D3 cause: frappe_docker nginx forces 'Origin: http://<site>' on /socket.io while Host=$host (localhost) → realtime authenticate fails 'Invalid origin'. Fix (dev only): bind-mount a copy of /templates/nginx/frappe.conf.template without the Origin override into erpnext-frontend via dev/compose.dialogbrain.yml. Avoid site-alias symlink (scheduler would see two sites). Carry D2+D3 into T6 dispatch.
Ruling D1 (confirmed for T8): attach erpnext backend + queue workers to the DialogBrain docker network via dev compose override (external network) so presigned minio URLs resolve; recreate only those app containers.
Ruling R8: T6 minors #1 (preserveFrom stuck → stops following new messages), #2 (client_ref reused after text change → new text silently not sent) and #4 (sending ref shared across conversations) are load-bearing for T7 realtime/send → carried into T7 dispatch as required fixes.
Ruling R9: notify.publish recipients = permitted_users(channel) ∪ enabled System Manager users (they can see everything); Notification Log recipients unchanged; update the Plan 1 test pinning recipients. — cost if wrong: extra realtime events to admins.
Ruling R10: customer card — "From chat" block (conversation contact_name/phone/email) and party block (party name + party's primary contact) labelled separately — goes into final fix wave. Ruling D4: dev/install.sh must use `up -d --no-deps` for named app services — final fix wave.
Ruling R11: media Range support (video seek/Safari) is a release blocker for voice/video but not for merge → own follow-up task before release (Plan 2b or Plan 4), recorded in follow-ups.

## Before release
- Media Range support (video seek, Safari audio/video) — R11.
- v15 bench run of the inbox (F1 aggregates, navbar badge) — Plan 4 CI matrix.

## Deferred minor findings
- Task 1: minor (deferred): no publish tests beyond R2; get_messages_by_name names uncapped; preview not unescaped vs text unescaped; set_value bumps modified; non-string filter values → 500.
- Task 1: minor (deferred): v15 sanitize_html disallowed_tags semantics unverified (CI Plan 4); svg <use href> survives.
- Task 2: minor (deferred): sequential placeholder substitution; desk note delete owner check when doc is str; no row lock on approve double-click.
- Task 2: minor (deferred): _is_gone matches 'not found' text (gateway sends error string as code); send_error not sanitised beyond key redaction; unknown timeouts not logged. T6 must handle approve/reject results status unknown|superseded and sent:false.
- Task 3: minor (deferred): DB connection held during stream; no overall stream deadline (gthread 8 slots); SSRF defence (allow_redirects=False, scheme check); gzip Content-Length mismatch; no Range support (video seek/Safari).
- Task 3: minor (deferred): Content-Type echoes raw mime (CR/LF → 500); un-iterated generator relies on GC to close upstream.
- Task 4: minor (deferred): user-permission restriction shows 0 orders not "no access"; party_name/lead/suggestions read without Customer/Lead perms; LIKE % _ unescaped in search_parties; float money.
- Task 6: minor (deferred): note drafts not per conversation (claim false); draft edge cases (approve superseded from edit clears text, startEdit drops files, dual busy guards); protocol-relative avatar URL; D3 hygiene (prod warning, stale comment, template fork drift); a11y (aria-pressed, focus trap); timers not cleared on unmount.
- Task 7: minor (deferred): badge refresh dropped while loading; sound in other tab while inbox focused elsewhere; silent:true doesn't stop 404/500 dialogs; auto mark_read checks visibility not focus; open-conv edit/remove doesn't refresh preview; paging drops in-flight row refresh; duplicate css class.
- Task 7: minor (deferred): possible 3rd badge on v16 if navbar selector also matches (check in T8); disabled-sysman test lacks non-empty guard; get_messages_by_name existence oracle (accepted); realtime_users duplicates _enabled_with_role, uncached; dead 2 s constant; PEP8 blank lines.
- Task 8: minor (deferred): badge 'again' queue untested (no JS harness); in_flight relies on claim bumping modified; external network must exist (comment); timings single run.
- Final: minor (deferred): markSelectedReadIfUnread uses stale unread when row not loaded; party id in header chip for users without Customer read (return party only with read, else party_linked flag); BS4 re-serialisation of linked messages.

## DialogBrain-side follow-ups
- Full AI draft text (agents.get_draft or no limit in list_drafts) — Edit is blocked on truncated drafts until then.
- Group chat sender names in message data.
