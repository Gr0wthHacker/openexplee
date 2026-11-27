# Self-hosting for cold outreach

This is the setup and running-cost guide for one business sending from its own outreach domains. Read [OPERATIONS.md](OPERATIONS.md) for deployment hardening and backups first.

## What runs where

- **App:** one FastAPI process with SQLite. It runs fine on a home PC that stays on, or on a small VPS. Keep the dashboard on loopback or behind a VPN.
- **Inbound mail (optional):** a Cloudflare Email Worker forwards replies to your inbox and posts them to `/api/cloudflare/inbound-webhook`. The Workers free tier covers this volume. Without it, the scheduler reads replies over IMAP.
- **Mailboxes:** real hosted mailboxes (Google Workspace, Microsoft 365, Zoho Mail or similar) on each outreach domain, added in the dashboard as SMTP inboxes with IMAP for reply checks.

## Domains and DNS

Send from secondary domains, never your main company domain. For each outreach domain:

1. **SPF:** one TXT record at the root that includes your mailbox provider, for example `v=spf1 include:_spf.google.com ~all`. If you also use Cloudflare Email Routing, merge both includes into the same single record. Two SPF records break SPF.
2. **DKIM:** turn DKIM signing on in the mailbox provider's admin console and publish the TXT record it gives you.
3. **DMARC:** start with `v=DMARC1; p=none; rua=mailto:<an address you read>` and move to `quarantine` once reports are clean.
4. **Redirect:** point the outreach domain's website at your real site so prospects who type it land somewhere real. The app does not set this up for you.
5. **MX:** the mailbox provider's MX records, unless you route inbound through Cloudflare Email Routing.

## Warmup and sending limits

The app has no warmup network. New mailboxes need a ramp before cold traffic:

- Weeks 1 to 2: normal mail only, a few real conversations a day with people who will reply.
- Weeks 3 to 4: 5 to 10 cold sends per mailbox per day.
- After that: up to 30 to 35 per mailbox per day (the default `daily_quota` is 35). Lower it per inbox in the dashboard if bounces or spam complaints rise.

A paid warmup service can replace the manual ramp, but it is usually the biggest line in the budget (see below).

## Leads

Discovery works from public web search and site scraping. It is useful for small lists. For a researched list, use `POST /api/campaigns/{id}/import` with `{"csv": "..."}`. Columns: `email` (required), `first_name`, `last_name`, `contact_name`, `company_name`, `company_domain`, `website_url`, `job_title`, `personalized_subject`, `personalized_body`, and an optional `verification_status` carried over from another verifier. Imported leads land as `enriched`. Nothing sends until an operator queues each lead, and only leads verified as `verified` or `deliverable` can be queued.

SMTP mailbox verification needs outbound port 25. Most home connections and many VPS providers block it, and then every lead comes back `risky`. Set `EMAIL_VERIFIER=millionverifier` with an API key to check addresses over HTTPS instead, verify the list elsewhere and import the result, or run the app on a host that allows port 25.

Names come from company sites. Without an LLM the app only finds "Name | Title" patterns. A local model through Ollama (`DEFAULT_LLM_PROVIDER=ollama`, `OLLAMA_MODEL=...`) costs nothing to run and finds far more.

To try discovery without sending anything or touching the app database:

```
python scripts/discovery_test.py "first search" "second search" --sites 15
```

It prints counts and the reasons sites failed, and writes per-site detail to `discovery-results.json`.

## Per-campaign settings

`PATCH /api/campaigns/{id}` sets the sending inbox, Reply-To, CC, booking link, sender name, a knowledge base for drafted replies, and the follow-up copy and delays. Follow-up templates take `$first_name`, `$company`, `$booking_link` and `$booking_line`, and `{a|b}` spintax. Unset fields use the defaults in `app/config.py`.

## Running cost

Prices as listed on vendor pages on 2026-11-27, per month, before tax.

| Item | Cheapest option | Cost |
| --- | --- | --- |
| App host | A PC you already run | $0 |
| App host | Hetzner CX23 VPS | €5.99 |
| Inbound routing | Cloudflare Workers free tier | $0 |
| Mailbox | Zoho Mail Lite 5 GB (annual billing only) | $1 per mailbox |
| Mailbox | Google Workspace Business Starter | $7 per mailbox |
| Warmup | Warmup Inbox Basic | $19 per mailbox |
| Domains | Renewal at your registrar | roughly $1 per domain |

Check that your mailbox provider's terms allow cold outreach before choosing it.
