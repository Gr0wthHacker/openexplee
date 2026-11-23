# OpenExplee security and reliability audit

Baseline `4120fafa97b2d53b569d50b7af55dba2100c814b`, the fork's original main branch.

The review covered application route registration, the dashboard's rendering and request paths, configuration and database migrations, public website discovery, SMTP verification and sending, inbound email processing, the Worker, local CLI and demo helpers, dependency manifests, deployment files, and operator documentation. Generated media and third-party package bodies were outside the manual source review. Local tests used temporary databases and mocked mail providers.

## Findings and remediation

| Severity | Baseline behavior and impact | Remediation |
| --- | --- | --- |
| Critical | `app/main.py`, `app/api/cli_hub.py`, and Compose exposed an unauthenticated API that accepted arbitrary executable arguments. CLI output and inherited application secrets were accessible to the process. | Removed the HTTP CLI router and administrative command execution. Added fail-closed operator authentication, same-origin checks for browser writes, and loopback publication. The shell-driven CLI is local. |
| High | `app/api/mailbox.py` and the dashboard copied replies to a hard-coded third-party address. The Worker defaulted to the same forwarding destination. Booking CTAs and sender identities defaulted to unrelated businesses. | Removed those runtime defaults and required an explicit Cloudflare forwarding destination. Manual replies use the original sender and configured CC. Reviewed draft text can still contain old destinations after an upgrade. |
| High | `app/engine/llm.py` automatically fell back to a hard-coded remote Ollama server and a local agent subprocess. Research, prospect details, and replies could leave the selected provider boundary. | Prompts go only to the selected supported provider. Removed the upstream private server configuration and model subprocess paths. |
| High | `app/engine/scraper.py` executed untrusted websites and followed redirects. SMTP verification connected to untrusted MX destinations. Both crossed the public-to-private network boundary. | Fetch bounded HTML without scripts and subresource loading. Resolve and connect to numeric public addresses. Recheck redirects. SMTP verification similarly connects to an approved public address. |
| High | `app/static/index.html` interpolated website, model, and mailbox strings directly into HTML and handler attributes, giving untrusted content a path into the operator dashboard. Runtime JavaScript loaded from public CDNs. | Escape displayed content, constrain link schemes, use numeric handler parameters and data attributes, and serve local UI assets with browser security headers. |
| High | `app/engine/sequencer.py` and `app/api/accounts.py` disabled SMTP certificate and hostname verification. Relay API credentials could travel over plaintext HTTP. | Enforce verified TLS for IMAP, verified TLS or STARTTLS for SMTP, and HTTPS for relay APIs. |
| High | `app/api/settings.py` advertised demo cleanup but deleted all leads and campaigns. The standalone seed script also deleted real data at its default path. | Removed the HTTP deletion and dashboard action. The seed script requires a new, explicitly separate database. |
| High | Sending relied on read-then-write state. Concurrent launches or schedulers could select duplicate leads, exceed inbox quota, and retry uncertain outcomes. | Claim campaign launch and lead delivery in SQLite. Reserve quota in the same transaction as lead claim. Move ambiguous deliveries to operator review and recover stranded claims on startup. |
| High | Discovery could dispatch model-written messages immediately. It supplied unrelated curated companies on weak matches and invented role-based addresses without website evidence. | Require approval before first send. Discovery drafts only, skips failed fetches, and drops the unconditional unrelated-company and role-address fallbacks. |
| Medium | Tagging overwrote the lead's lifecycle state, hiding threads. Manual replies selected the first inbox and the dashboard did not display stored conversation messages. | Store tags separately, migrate known legacy tags, display message history, and use the original inbox and available thread reference. |
| Medium | SMTP recipient rejections raised exceptions that the verifier treated as inconclusive. Free-form errors could trigger suppression based on unrelated server text. | Handle structured SMTP refusal codes. Suppress only a permanent refusal for the actual recipient. |
| Medium | The inbound Worker reused its send bearer for callback authentication, and webhook retries could process a message again after a competing insert. | Separate the credentials and stop when a duplicate inbound row is rejected by SQLite. |
| Medium | Build context included operating files, the application container ran as root, and undocumented dependency drift affected repeatability. | Exclude local secrets and data, copy only runtime sources into the image, run with a dedicated UID and dropped capabilities, and pin verified runtime versions. |
| Medium | The only baseline check script was not discovered by pytest and failed when run directly because it omitted the webhook credential. | Make it a collected test with an isolated database and add focused security and campaign regression tests. |
| Medium | Simulated inbox warmup changed a health score without sending or measuring delivery. The dashboard claimed a fixed dollar saving per message. | Removed the simulation and hypothetical savings from the dashboard. DNS checks report the records they actually inspect. |
| Medium | DNS setup could add routing records over an existing mail configuration. | Verify the zone and reject automatic setup when existing MX, SPF, or DMARC records need review. |

## Phishing and data routing

The baseline hard-coded CC, forwarding, booking, sender identity, and LLM destinations have concrete data-routing effects without requiring proof of author intent. This audit found no evidence that those destinations were actually used by any installed instance. If the baseline was deployed, inspect copies of provider send logs and forwarding rules, confirm where replies and prompts went, and rotate credentials if the unauthenticated service was reachable.

## Comparable open-source projects

- **Mautic** supports explicit campaign workflows and separates campaign management from outbound execution. OpenExplee follows the review-before-dispatch pattern while keeping the existing SQLite application. [Mautic](https://github.com/mautic/mautic)
- **listmonk** uses explicit list and campaign state and supports subscriber exports. OpenExplee adds a campaign-scoped CSV export with spreadsheet formula escaping. [listmonk](https://github.com/knadh/listmonk)

## Remaining boundaries

- The supported configuration is a single operator per database and one application process. Multi-user roles, tenant isolation in one process, and distributed scheduling require architectural changes.
- Provider API contracts, production reverse proxy settings, approved DNS changes, and successful real email delivery need to be checked in the target environment. The application cannot establish whether campaign recipients consented, and SMTP mailbox checks do not prove delivery.
- The app still stores credentials in SQLite and intentionally permits an authenticated operator to configure trusted local services. Use protected volumes, constrained service networks, and credentials scoped to the intended providers.
- SMTP acceptance can race with a process crash or inbound opt-out. Provider-side idempotency is absent. Ambiguous delivery requires operator reconciliation. Follow-up and automatic reply automation carry separate provider and mailbox dependencies.
- The optional Mailflare and Hyvor services, their production deployments, and all upstream third-party source code were outside verification.
