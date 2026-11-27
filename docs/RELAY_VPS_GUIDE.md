# Self-Hosted Hyvor Relay (`vikasvardhanv/relay`) VPS Guide

Use Hyvor Relay on a \$5–\$6/month Linux VPS when you want unlimited outbound sending across multiple secondary domains without Google, Resend, SES, or Cloudflare rate limits or bans.

---

## 1. VPS Requirements

1. Rent an Ubuntu 24.04 VPS (\$5–\$6/month) from a provider that allows outbound TCP port 25 (or opens it on request, such as Hetzner, Vultr, or Contabo).
2. Make sure the VPS provider sets a **PTR (Reverse DNS)** record on your VPS IP pointing to your Relay hostname (for example, `relay.example-outreach.com`).

---

## 2. Start Relay with Docker Compose

1. Copy `infra/relay/` to your VPS.
2. Create the `.env` file and set secrets:
   ```bash
   cd infra/relay
   cp .env.example .env
   # Generate APP_SECRET with: openssl rand -hex 32
   ```
3. Start Postgres and Relay:
   ```bash
   docker compose up -d
   ```
4. Open `WEB_URL` in your browser, log in via OIDC, create a **Project** (`Distributional` or `Transactional`), add your secondary domain (e.g., `example-outreach.com`), and publish the DKIM/SPF/DMARC records shown by Relay.
5. Generate an **API Key** inside your Relay Project.

---

## 3. Connect Relay to OpenExplee

1. Open OpenExplee (`http://localhost:8000`) &rarr; **Inboxes & Domains**.
2. Select **Provider:** `Hyvor Relay (Self-Hosted SMTP)`.
3. Fill in:
   * **Email Address:** `alex@example-outreach.com` (your secondary domain — never your main company domain)
   * **Sender Name:** `Cristy Krall`
   * **SMTP Host:** `relay.example-outreach.com`
   * **SMTP Port:** `587`
   * **SMTP Username:** `relay` (any non-empty string; Relay authenticates using the password token)
   * **Password:** Paste your **Hyvor Relay API Key**
   * **Reply-To Address:** the address on your main domain where you want replies (optional)
   * **Master Notification CC Email:** leave empty unless you want a copy of every send
4. Click **Add & Authenticate Inbox**, then click **Test SMTP**.

