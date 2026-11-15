# DialogBrain for ERPNext

Omnichannel inbox that mirrors DialogBrain conversations into ERPNext.

## Development

From the frappe_docker directory: `bash dialogbrain_erpnext/dev/install.sh`.
Run tests: `sudo docker exec erpnext-backend-1 bench --site test.localhost run-tests --app dialogbrain`

## Known limitations

- **Private mode latency is about 5 minutes.** In private mode (no inbound webhooks)
  reconcile polls every minute, but DialogBrain caches `search.threads` and
  `messages.read_history` results for API keys for 5 minutes, so a new chat or message
  can take up to ~5 minutes to appear. It drops to about a minute once DialogBrain
  bypasses that cache for API keys (planned DialogBrain change, Plan 3). The scheduler
  tick must also be 60 s: `bench set-config -g scheduler_tick_interval 60`, then restart
  the scheduler (DialogBrain Settings shows this warning when needed).
- **`contact.updated` does nothing yet.** DialogBrain's message events don't carry the
  contact id, so conversations never get `db_contact_id` and contact name/phone changes
  are not applied. Fixed once DialogBrain sends `contact_id` in message events
  (planned DialogBrain change, Plan 3).
- **Agents change only the status of a conversation directly.** Assignment, party link
  and other actions go through server methods (Plan 2 inbox actions); Chat Managers can
  edit those fields.
- **A deleted webhook needs a Reconnect.** If the subscription is deleted on the
  DialogBrain side, Settings shows "missing … run Reconnect" and the site polls every
  5 minutes until then.

## License

MIT
