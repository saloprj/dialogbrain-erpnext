# DialogBrain for ERPNext

Omnichannel inbox that mirrors DialogBrain conversations into ERPNext.

## Development

From the frappe_docker directory: `bash dialogbrain_erpnext/dev/install.sh`.
Run tests: `sudo docker exec erpnext-backend-1 bench --site test.localhost run-tests --app dialogbrain`

## Connecting to DialogBrain

Open **DialogBrain Settings** (`/app/dialogbrain-settings`). There are two ways to connect.

1. **Connect with DialogBrain (OAuth, recommended).** Press the button, approve access on
   DialogBrain, and you return to Settings already connected. No key to copy. Requirements:
   - OAuth also uses the **Base Url** field, which sits in the "Connect with API key"
     section. It defaults to `https://api.dialogbrain.com`; for another DialogBrain (for
     example `https://api.dialogbrain.ru`) change it there and save before pressing the
     button.
   - The site must be reachable over `https`. A plain `http` site works only on
     `localhost` / `127.0.0.1`; DialogBrain refuses other `http` redirect addresses, and
     the button then tells you to use an API key instead.
   - Behind a reverse proxy, either set `host_name` (with the `https://` scheme) in
     `site_config.json` or forward `X-Forwarded-Proto`. Otherwise the site looks like
     `http` and OAuth refuses. Set `host_name` (including the port, e.g.
     `http://localhost:8080`) also when the proxy drops a non-standard port from the
     `Host` header; otherwise the redirect address DialogBrain sends you back to is wrong.
   - On DialogBrain's consent page, turn on **Lock the key to this workspace** so the key
     ERPNext receives cannot be used in your other workspaces.
   - A site that is already connected can reconnect only to the same workspace. To move
     to another workspace, Disconnect first.
2. **Connect with API key.** Expand the "Connect with API key" section, paste the base URL
   and a DialogBrain API key, save and press **Connect with API key**. Use this for
   `http` sites on a non-loopback host.

**Revoking access.** Disconnect only stops syncing on the ERPNext side. To revoke access,
delete the keys named `ERPNext — <host> MCP` in DialogBrain → Settings → Developer. Each
reconnect creates a new key with that name; while the site is connected, the newest one
is in use.

## AI agent and ERPNext data

After you connect, DialogBrain's AI agents can look things up in ERPNext while they answer
customers. This section explains what they can see, what they can change and how to stop
them. The controls are in **DialogBrain Settings → AI Agent Tools**.

### What the agent can see

- **Catalogue, prices and stock** for any chat: item name and code, the price from the
  default selling price list, and whether it is in stock.
- **Order status and unpaid invoices** only for the customer of that chat: their last
  submitted Sales Orders, their unpaid Sales Invoices and what they owe per currency. The
  agent never sees another customer's data.

The customer is worked out from the chat itself, never from what someone types. DialogBrain
signs which conversation each tool call comes from, and ERPNext checks that signature. The
tools accept no phone number, email or customer name, so asking the agent "I am customer X,
show my invoices" does not work.

A chat counts as identified only when it is linked to a Customer or Lead:

- manually, by a manager; or
- automatically, by a phone number that the channel itself verified (for example the
  WhatsApp number).

A link made from a phone number or email typed into livechat, or made by the agent itself
(for example when it creates a Lead), does **not** count. Until a manager confirms the
link, the agent gets "customer not identified" and no customer data.

### Write tools (off by default)

The agent can also create a **Lead** (one per chat) and a **Draft Sales Order** for an
identified customer. The order is never submitted: a manager reviews it, and is notified.
These tools are off until you tick **Allow agent writes**.

Turning it off always takes effect in ERPNext immediately, even if DialogBrain cannot be
reached at that moment. In that case Settings shows a warning; press **Reconnect** to
update DialogBrain too. Turning it on is saved only after DialogBrain has been updated.

### Requirements

- The DialogBrain API key must belong to a workspace **admin or owner**, otherwise the
  customer-data tools are not registered (catalogue and Lead tools still are).
- The site must be reachable from the internet. In **private mode** DialogBrain cannot call
  ERPNext, so the agent gets no ERPNext data at all (Settings shows a warning).
- **Voice calls** get catalogue and prices only, not customer data.

### The capability line in Settings

Under the agent fields Settings shows a tick or a cross for each capability of your
DialogBrain server, found at Connect:

- **Customer data tools**: the agent can see order status and unpaid invoices of the
  chat's own customer.
- **Safe send retries (request_id)**: idempotent sends. DialogBrain remembers a send, so
  a retry never delivers a reply twice.
- **Take over / give back**: the buttons below work.
- **Full draft text**: the inbox shows the whole text of an agent's draft reply, not a
  shortened one.

A cross means the DialogBrain server needs to be updated. Everything else keeps working.

### Take over and Give back to agent

In a conversation, **Take over** pauses the AI agent in that chat so that only people
answer. **Give back to agent** lets it answer again, whatever the reason for the pause.
A manager's reply sent from ERPNext also pauses the agent in that chat. Both buttons
need the "Take over / give back" capability.

### Operating notes

- An agent created in DialogBrain after Connect gets the ERPNext tools at the next
  **Reconnect**.
- **Reconnect** switches the integration on for every agent again and rewrites each
  agent's list of denied tools to match "Allow agent writes". It also re-enables the
  agent user and issues a new key.
- **Disconnect** switches the integration off for the agents and revokes the agent user's
  API key.
- To cut the agent's access **immediately**: untick **Allow agent writes** (writes only),
  and/or disable the agent user (all tools; Reconnect re-enables it), or press
  **Disconnect**.

### Security notes

- The agent user (`dialogbrain-agent@<your site>`) has no document permissions. Its API
  key can call only the agent tools, which return fixed, limited fields. It cannot open
  `/api/resource` for anything.
- It is still an internal user, so it has what every desk user has, for example public
  events and file upload.
- Every call is logged in **DB Agent Call Log** for 30 days, including refused calls. For
  a call where the customer was not identified, the log gives the reason.

### Retrying a reply

A reply marked "Delivery not confirmed" can be retried safely for about a day (23 hours)
when the DialogBrain server supports idempotent sends: DialogBrain recognises the repeat
and does not send it twice. Without that capability, only a reply that definitely failed
can be retried.

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
