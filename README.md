# DialogBrain for ERPNext

Omnichannel inbox that mirrors DialogBrain conversations into ERPNext.

## Development

From the frappe_docker directory: `bash dialogbrain_erpnext/dev/install.sh`.
Run tests: `sudo docker exec erpnext-backend-1 bench --site test.localhost run-tests --app dialogbrain`

## Connecting to DialogBrain

Open **DialogBrain Settings** (`/app/dialogbrain-settings`). There are two ways to connect.

1. **Connect with DialogBrain (OAuth, recommended).** Press the button, approve access on
   DialogBrain, and you return to Settings already connected. No key to copy. Requirements:
   - The site must be reachable over `https`. A plain `http` site works only on
     `localhost` / `127.0.0.1`; DialogBrain refuses other `http` redirect addresses, and
     the button then tells you to use an API key instead.
   - Behind a reverse proxy, either set `host_name` (with the `https://` scheme) in
     `site_config.json` or forward `X-Forwarded-Proto`. Otherwise the site looks like
     `http` and OAuth refuses.
2. **Connect with API key.** Expand the "Connect with API key" section, paste the base URL
   and a DialogBrain API key, save and press **Connect with API key**. Use this for
   `http` sites on a non-loopback host.

**Revoking access.** Disconnect only stops syncing on the ERPNext side. To revoke the key,
go to DialogBrain, Settings, Developer, and delete the key named `ERPNext — <host> MCP`.

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
