# OpenExplee

OpenExplee is a self-hosted dashboard for researching B2B prospects, reviewing outreach drafts, managing sending inboxes, and handling replies.

## Start with Docker Compose

```sh
cp .env.example .env
chmod 600 .env
openssl rand -hex 32
```

Put the generated password in `OPENEXPLEE_ADMIN_PASSWORD` in `.env`. Configure an LLM key if you want model-assisted research and drafting, then run:

```sh
docker compose up --build -d
docker compose ps
```

Open `http://127.0.0.1:8000` and sign in as `admin` with your configured password. This installation is intended for one operator or one trusted team. Use a separate installation and volume for each business that needs independent access, credentials, or suppression lists.

## Review and send a campaign

1. Open **Inboxes & Domains** and connect a sending account your business owns. Test its connection.
2. Open **Campaigns** and provide an ideal customer profile, or use a public product website to generate campaign drafts.
3. Open **Review leads** on a campaign. Review the recipient, company, and draft. The saved verification check can be refreshed from the lead table. Approve each eligible lead you intend to send to.
4. Return to **Campaigns** and select **Send Batch**. The dashboard sends up to 20 approved leads per launch, subject to remaining inbox quotas.
5. Use **Mailbox** to read and tag conversation history. Use the **CSV** link on a campaign to export its lead records.

The crawler reads public HTML, checks redirects, and extracts available metadata and contacts. Sites that require JavaScript may return sparse results. SMTP checks provide evidence of mailbox acceptance. Catch-all, temporary failures, and blocked verification are shown separately.

## Operations

- [Deployment, upgrades, backups, and recovery](docs/OPERATIONS.md)
- [Security audit and remediation](docs/SECURITY_AUDIT.md)
- [Cloudflare Worker and Mailflare configuration](docs/CLOUDFLARE_WORKERS_GUIDE.md)

## Local development

Use Python 3.11 or later.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt pytest
python -m pytest -q
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

The application reads `.env` from the current directory. For local Ollama outside Docker, set `OLLAMA_BASE_URL=http://localhost:11434`. The optional local CLI reads the same database. Commands that use CLI-Anything require an independently installed `cli-hub` binary.

The dashboard bundles Tailwind CSS 3.4.19 output and Lucide 0.468.0. Their licenses are in `app/static`. To regenerate CSS after changing classes, create a stylesheet containing `@tailwind base;`, `@tailwind components;`, and `@tailwind utilities;`, then run Tailwind 3.4.19 with `--content app/static/index.html --minify`.

[MIT license](LICENSE)
