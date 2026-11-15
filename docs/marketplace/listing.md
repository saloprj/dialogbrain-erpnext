# Frappe Cloud Marketplace listing — DialogBrain

Draft text for the Marketplace app page and for the awesome-frappe entry. Screenshots are in
`docs/marketplace/screenshots/` and show a demo site with made-up companies and people.

## Title

DialogBrain

## Subtitle (short description, one line)

WhatsApp, Telegram, email and website chat in ERPNext, with an AI agent that knows your customers and orders.

## Categories

Sales, CRM, Customer Support, Integrations

## Description

DialogBrain brings every customer conversation into ERPNext. WhatsApp, Telegram, Instagram,
Facebook, email, website live chat and other channels connected in your DialogBrain workspace
land in one **Chats** inbox inside the Desk, next to your customers, leads, quotations and orders.

**One inbox for the whole team**

- All channels in one list, with filters for unanswered, unassigned, mine and "needs a human".
- Assign chats to managers, leave internal notes, use quick replies.
- New messages arrive in real time, with an unread badge and a sound.
- Chat Agents see only the channels they are allowed on; Chat Managers see everything.

**Linked to your ERPNext data**

- Each chat is linked to its Customer or Lead by phone number or email, automatically.
- The customer card next to the chat shows recent orders and unpaid invoices.
- Create a Lead, a Quotation or a Sales Order from the chat in one click.

**AI agent per channel**

- Choose for each channel: **Copilot** (the agent drafts, a person approves), **Autopilot**
  (the agent answers by itself) or **Off**.
- The agent reads ERPNext through scoped tools: products with prices and stock, and only this
  customer's orders and unpaid invoices. It can create leads and draft orders, never submit them.
- Take over a chat at any moment and give it back to the agent later.

**Reports**

- First response time, unanswered conversations, conversations by channel and by manager.

**Setup in two minutes**

Install the app, open DialogBrain Settings and click **Connect with DialogBrain** (OAuth),
or paste an API key. The last weeks of history load in the background.

Works on Frappe / ERPNext **v15 and v16**. English, Russian and Ukrainian.

## Requirements

- ERPNext v15 or v16, scheduler and background workers running.
- A DialogBrain workspace (https://dialogbrain.com). Channels are connected and billed in DialogBrain.
- For the AI agent: the site must be reachable from the internet over https.

## Links

- Website: https://dialogbrain.com
- Source: https://github.com/saloprj/dialogbrain-erpnext
- Documentation: https://github.com/saloprj/dialogbrain-erpnext#readme
- Support: support@dialogbrain.com
- Privacy policy: https://dialogbrain.com/privacy
- Terms of service: https://dialogbrain.com/terms

## Pricing

Free app. DialogBrain itself is a paid service with its own plans.

## awesome-frappe entry (Apps → Integrations)

```
- [DialogBrain](https://github.com/saloprj/dialogbrain-erpnext) - Omnichannel inbox (WhatsApp, Telegram, email, live chat) with an AI agent, linked to ERPNext customers and orders.
```
