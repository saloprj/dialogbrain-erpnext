# DialogBrain for ERPNext

Omnichannel inbox that mirrors DialogBrain conversations into ERPNext.

## Installation

Requirements: a bench with Frappe and ERPNext **v15 or v16** (Python 3.10+), the scheduler
and background workers running, and a DialogBrain workspace.

```bash
cd frappe-bench
bench get-app https://github.com/saloprj/dialogbrain-erpnext
bench --site your-site install-app dialogbrain
bench --site your-site migrate
bench --site your-site clear-cache
bench restart          # production setups (supervisor/systemd); in docker restart the backend, workers and scheduler
```

`your-site` is the site's name in `sites/`. `get-app` builds the app's assets; if you build
them again later, clear the cache afterwards, otherwise the Chats page opens blank.

Then:

1. **Give people access.** The app adds the roles **Chat Agent** (answers chats on channels
   whose allowed users include them, or that list nobody) and **Chat Manager** (all chats, agent modes, reports).
   Assign them to users; System Managers see everything.
2. **Connect.** Open **DialogBrain Settings** (`/app/dialogbrain-settings`) and connect with
   DialogBrain (OAuth) or with an API key — see [Connecting to DialogBrain](#connecting-to-dialogbrain).
   For the AI agent to read ERPNext data the key must belong to a workspace admin or owner,
   and the site must be reachable from the internet over `https`.
3. **Open the inbox.** Chats are under **DialogBrain → Chats** (`/app/dialogbrain-inbox`). The
   first sync loads recent history in the background; channels appear as **DB Channel**,
   where each channel's allowed users and AI agent mode are set.

To update: `bench update --apps dialogbrain` (or `git pull` in `apps/dialogbrain`, then
`bench --site your-site migrate`, `bench build --app dialogbrain`, `bench --site your-site clear-cache`).

## Development

From the frappe_docker directory: `bash dialogbrain_erpnext/dev/install.sh`.
Run tests: `sudo docker exec erpnext-backend-1 bench --site test.localhost run-tests --app dialogbrain`
After `bench build`, run `bench --site <site> clear-cache` (install.sh does): the cache keeps
the old assets manifest and the inbox opens blank.

The dev stand's frontend is also reachable from the DialogBrain stack as
`http://erpnext-dev:8080` (network alias in `dev/compose.dialogbrain.yml`). For a DialogBrain
that has to call ERPNext, `install.sh` prints two manual steps: `SSRF_ALLOW_HTTP_HOSTS=minio,erpnext-dev`
on DialogBrain (it replaces the default `minio`, keep both) with an `api-gateway` restart,
and the site's Public site URL set to `http://erpnext-dev:8080`.

## AI agent mode per channel

In DialogBrain an AI agent answers a channel through an incoming-message trigger. DialogBrain
owns that setting; ERPNext reads it and lets a manager switch it. Each **DB Channel** shows
its agent and mode (read-only fields, plus a line at the top of the form).

| Mode | What the agent does |
|---|---|
| **Copilot** | writes a draft reply; a person approves it |
| **Autopilot** | may answer by itself. DialogBrain's own safety gate can still hold a single answer as a draft; ERPNext does not see that |
| **Off** | the trigger is disabled: the agent does not answer this channel |

On the DB Channel form, the **DialogBrain agent** button group has **Copilot**, **Autopilot**
and **Off** (when the channel has an agent to switch), **Choose agent…** and **Refresh from
DialogBrain**. **Choose agent…** is always there but offers a choice only when several
agents answer the channel; otherwise it says why there is nothing to choose. The current
agent and mode are in the line at the top of the form.

- **Who:** Chat Managers and System Managers only; others do not see the buttons and the
  server refuses the call.
- **Shared trigger rule:** a channel is switched only when its trigger serves that channel
  alone. If the trigger also serves other channels, ERPNext does not switch it (that
  would change the other channels too) and says "The agent trigger also serves other
  channels: change it in DialogBrain". The same applies to a trigger that lists no channel
  at all (it answers every channel, or every channel of some types): it is shown, never
  switched. If several agents answer one channel, use **Choose agent…**.
- **Another trigger also answers:** when the channel's own trigger is switched but another
  enabled trigger (a second agent, a shared or an every-channel trigger) answers the same
  channel, the form says "Another agent trigger also answers this channel: Off here does not
  stop it". The switch still happens; to silence the channel, change the other trigger in
  DialogBrain.
- **Key:** the DialogBrain Connect key must belong to a member, admin or owner of the
  workspace; otherwise agents cannot be read ("connect with a member, admin or owner key").
- **Refresh:** modes are read from DialogBrain every hour and when someone presses
  **Refresh from DialogBrain**. A switch made here is stored only after DialogBrain
  accepted it.

## Reports

Three script reports under the DialogBrain workspace, for Chat Managers and System
Managers only:

- **First Response Time:** per manager, channel or day: conversations, answered, not
  answered yet, average / median / longest minutes and the share answered within 5
  minutes. Filters: period, group by, channel, assigned to. "Manager" is the person the
  conversation is assigned to, not the one who typed the first reply.
- **Unanswered Conversations:** open conversations whose last message is from the
  customer, longest waiting first. Filters: channel, assigned to, minimum minutes waiting.
- **Conversations by Channel and Manager:** opened and closed in the period, open and
  unanswered now, inbound and outbound messages, and how many were sent by AI. Filters:
  period, group by (channel or manager).

## Languages

The app is available in English, Ukrainian and Russian (`dialogbrain/translations/uk.csv`
and `ru.csv`); the language follows the user's setting. The app's own interface strings
that would otherwise share their wording with ERPNext's carry their own translation
context ("DialogBrain"), so translating them does not change ERPNext's wording elsewhere.

## Supported versions and tests

| Frappe / ERPNext | Python | Node | MariaDB |
|---|---|---|---|
| v15 | 3.10+ (tested on 3.10) | 18+ | 10.6 |
| v16 | 3.14 | 24+ | 11.8 |

The same code runs on both; the few API differences are handled in
`dialogbrain/compat.py`.

- **v16:** the dev bench above (`erpnext-backend-1`), command as in Development.
- **v15:** `dev/test-v15.sh` runs the whole suite on Frappe/ERPNext v15 in throwaway
  containers (`mariadb:10.6`, `redis:7`, `frappe/erpnext:v15`, all named `dbx-v15-<pid>`,
  on their own network, no published ports, removed on exit, also on Ctrl-C / SIGTERM). The repo is not
  mounted: its tree is streamed into the container with `tar` (without `.git` and any
  `*.env` file), so the run never writes into your checkout and never sees the dev key. The image ships
  Python 3.11; to test on 3.10 the script rebuilds the bench virtualenv on a Python 3.10
  from the host (`DBX_V15_PYTHON=/path/to/python3.10`, or found with
  `uv python find 3.10`; get one with `uv python install 3.10`). Set
  `DBX_V15_PIP_CACHE=<dir>` to keep downloaded wheels between runs. The exit status is
  that of `bench run-tests`; the last lines of the test log are printed.
- **CI:** `.github/workflows/ci.yml` runs the suite on every push and pull request for
  both versions (v15 on Python 3.10, v16 on Python 3.14).

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
   - While the site is connected, the API key and the Base Url cannot be changed: press
     **Disconnect** first.
   - Changing the DialogBrain address while the site holds mirrored chats: Connect (either
     way) first asks whether it is **the same DialogBrain server and workspace at a new
     address**. *Yes*: the existing chats are moved to the new address and keep syncing;
     their verified customer links become unverified, so the AI agent sees those
     customers' data again only after a manager confirms the link (or the phone match
     verifies it again). *No*, or a key of another workspace: Connect refuses and writes
     nothing. Chats from a different DialogBrain server or workspace cannot be mixed into
     one site (their thread ids collide with the old ones): use a new site, or delete the
     mirrored chats (DB Conversation) first.
2. **Connect with API key.** Expand the "Connect with API key" section, paste the base URL
   and a DialogBrain API key, save and press **Connect with API key**. Use this for
   `http` sites on a non-loopback host.

**Revoking access.** Disconnect only stops syncing on the ERPNext side. To revoke access,
delete the keys named `ERPNext — <host> MCP` in DialogBrain → Settings → Developer. Each
reconnect creates a new key with that name; while the site is connected, the newest one
is in use.

## Connecting a channel from ERPNext

An employee can link their own WhatsApp or Telegram number without a DialogBrain login:
**Chats → menu → Connect channel**, or the **Connect channel** button on the DB Channel list.

- **Who:** Chat Agent, Chat Manager and System Manager. An agent connects only their own
  number. A manager can also connect a number for an employee, or as a shared number.
- **How:** scan a QR code; or, where DialogBrain offers it, enter a pairing code on the phone
  (WhatsApp) or sign in with the phone number, the login code and the two-step password
  (Telegram). A connection that is not finished in a few minutes expires; start it again.
- **Whose chats:** the new channel is assigned to its owner (the channel's **Allowed users**),
  so only that employee and the managers see its chats. A shared number has no allowed users
  and every agent sees it. A manager can change this later on the DB Channel form. This
  holds even if the browser is closed right after the scan: the assignment is finished on
  the server within about a minute.
- **Managers only:** a number that shows up while someone is connecting one, before ERPNext
  knows whose it is, is created with **Managers only** ticked: no agent sees it. It goes to
  its owner as soon as DialogBrain says which connection it was, and it opens up if
  DialogBrain says it was nobody's (a number connected in DialogBrain itself). If DialogBrain
  could not be asked, it stays for managers only: choose its allowed users, or untick the
  box, on the DB Channel form.
- A number that is already a shared channel is not taken away from the other agents when
  one of them scans it again; a manager assigns it.
- **In DialogBrain** the account belongs to the workspace of the site's API key, not to the
  employee. "Only the owner and managers" is an ERPNext rule.
- The phone number, the login code and the password are passed to DialogBrain once and are
  never stored or logged in ERPNext.

This needs a DialogBrain server with the `channels.connect_session.*` tools and a key with
edit rights in the workspace. Until the server has them, the dialog says so and nothing else
changes; the app checks every hour, and at Connect / Re-sync. Disconnecting a number is done
in DialogBrain.

To look at the dialog without a real DialogBrain, `dev/fake_connect_server.py` plays the
server's part on a dev site (see the file's header).

## AI agent and ERPNext data

After you connect, DialogBrain's AI agents can look things up in ERPNext while they answer
customers. This section explains what they can see, what they can change and how to stop
them. The controls are in **DialogBrain Settings → AI Agent Tools**.

### What the agent can see

- **Catalogue, prices and stock** for any chat: item name and code, the price from the
  default selling price list, and whether it is in stock. The stock is the sum over all
  warehouses of the company; the price is the plain rate on the default selling price
  list (no pricing rules, no other units of measure).
- **Order status and unpaid invoices** only for the customer of that chat: their last
  submitted Sales Orders, their unpaid Sales Invoices and what they owe per currency. The
  agent never sees another customer's data.

The customer is worked out from the chat itself, never from what someone types. DialogBrain
signs which conversation each tool call comes from, and ERPNext checks that signature. The
tools accept no phone number, email or customer name, so asking the agent "I am customer X,
show my invoices" does not work.

A chat counts as identified only when it is linked to a Customer or Lead:

- manually, by someone who can read that customer in ERPNext; or
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
- **Safe send retries (request_id)**: idempotent sends. DialogBrain remembers a send that
  completed, so a retry of it is not delivered again.
- **Take over / give back**: the buttons below work.
- **Full draft text**: the whole text of an agent's draft reply is loaded when you press
  **Edit** (the list shows a shortened preview).

A cross means the DialogBrain server needs to be updated. Everything else keeps working.
The line is worked out at Connect: after DialogBrain is upgraded, press **Reconnect** to
pick up the new capabilities.

### Take over and Give back to agent

In a conversation, **Take over** pauses the AI agent in that chat so that only people
answer. **Give back to agent** lets it answer again, whatever the reason for the pause.
A manager's reply sent from ERPNext also pauses the agent in that chat, but only when the
DialogBrain server has the "Safe send retries" capability. Both buttons need the
"Take over / give back" capability.

### Operating notes

- An agent created in DialogBrain after Connect gets the ERPNext tools at the next
  **Reconnect**.
- **Reconnect** switches the integration on for every agent again and rewrites each
  agent's list of denied tools to match "Allow agent writes". It also re-enables the
  agent user and issues a new key. Toggling **Allow agent writes** does the same for the
  agents: it also switches the integration back on for every agent.
- **One ERPNext site per DialogBrain workspace.** A connected site reconnects only to the
  same workspace. A copy of the site (for example a restored backup at another address)
  registers its own integration at Connect instead of taking over the original's. A site
  never mixes chats of two DialogBrain servers or workspaces: Connect refuses while chats
  of another one are mirrored (see "Connecting to DialogBrain"), and a leftover chat of
  another one is never identified to the agent.
- Draft Sales Orders created by the agent are inserted as **Administrator**: hooks and
  server scripts on Sales Order insert run as Administrator for them.
- **Disconnect** switches the integration off for the agents and revokes the agent user's
  API key.
- To cut the agent's access **immediately**: untick **Allow agent writes** (writes only),
  and/or disable the agent user (all tools; Reconnect re-enables it), or press
  **Disconnect**.

### Security notes

- The agent user (`dialogbrain-agent@<your site>`) has no document permissions of its
  own: through the standard API it cannot read customers, orders, invoices or items. The
  agent tools return fixed, limited fields.
- It is still an internal user, so it has what every desk user has, for example file
  upload, ToDo and public notes.
- Every call is logged in **DB Agent Call Log** for 30 days, including calls refused to
  the agent user or to an API key. For a call where the customer was not identified, the
  log gives the reason. The log never blocks deleting a Customer or Lead.
- Uninstalling the app removes the `DialogBrain Conversation` fields, disables the agent
  user, clears its key and (when connected) switches the integration off for the agents.

### Retrying a reply

A reply marked "Delivery not confirmed" can be retried for about a day (23 hours) when
the DialogBrain server supports idempotent sends. A retry is deduplicated by DialogBrain
when the earlier send completed; in rare cases (DialogBrain lost its cache, or two
attempts overlap) a message can still be delivered twice. Without that capability, only a
reply that definitely failed can be retried.

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
