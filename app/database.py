import aiosqlite
from pathlib import Path
from app.config import settings

DB_FILE = Path(settings.database_path)


async def init_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    DB_FILE.touch(mode=0o600, exist_ok=True)
    DB_FILE.chmod(0o600)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute("BEGIN IMMEDIATE")

        # Campaigns Table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            icp_prompt TEXT NOT NULL,
            target_industry TEXT,
            status TEXT DEFAULT 'draft',
            value_proposition TEXT,
            reply_to_email TEXT,
            cc_email TEXT,
            total_leads INTEGER DEFAULT 0,
            sent_count INTEGER DEFAULT 0,
            hot_leads_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Leads Table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
            company_name TEXT,
            company_domain TEXT,
            website_url TEXT,
            contact_name TEXT,
            first_name TEXT,
            last_name TEXT,
            job_title TEXT,
            email TEXT,
            verification_status TEXT DEFAULT 'unverified',
            verification_reason TEXT,
            personalized_subject TEXT,
            personalized_body TEXT,
            status TEXT DEFAULT 'discovered',
            is_hot_lead BOOLEAN DEFAULT 0,
            sent_at TIMESTAMP,
            sent_from_inbox TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Inboxes / Sending Accounts Table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS inboxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            display_name TEXT,
            provider TEXT NOT NULL,
            smtp_host TEXT NOT NULL,
            smtp_port INTEGER NOT NULL DEFAULT 587,
            smtp_user TEXT NOT NULL,
            smtp_password TEXT NOT NULL,
            reply_to TEXT,
            cc_email TEXT,
            use_tls BOOLEAN DEFAULT 1,
            daily_quota INTEGER DEFAULT 35,
            sent_today INTEGER DEFAULT 0,
            last_sent_at TIMESTAMP,
            status TEXT DEFAULT 'active',
            health_score INTEGER DEFAULT 100,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Warmup Log Table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS warmup_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inbox_id INTEGER REFERENCES inboxes(id) ON DELETE CASCADE,
            action TEXT,
            peer_address TEXT,
            status TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # App Settings Key-Value Table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)

        # Suppression / Deduplication List Table (emails & domains never to contact)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS suppression_list (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_type TEXT NOT NULL DEFAULT 'email',
            value TEXT UNIQUE NOT NULL,
            reason TEXT DEFAULT 'manual',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Conversation history: every email in or out, one row each
        await db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,
            direction TEXT NOT NULL,
            message_id TEXT UNIQUE,
            subject TEXT,
            body TEXT,
            intent TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Safe Schema Migrations for existing DBs
        for table, col, col_type in [
            ("campaigns", "reply_to_email", "TEXT"),
            ("campaigns", "cc_email", "TEXT"),
            ("campaigns", "inbox_id", "INTEGER"),
            ("campaigns", "booking_url", "TEXT"),
            ("campaigns", "hot_leads_count", "INTEGER DEFAULT 0"),
            ("leads", "mailbox_tag", "TEXT"),
            ("leads", "dispatch_claim", "INTEGER"),
            ("leads", "is_hot_lead", "BOOLEAN DEFAULT 0"),
            ("leads", "followup_step", "INTEGER DEFAULT 0"),
            ("leads", "next_followup_at", "TIMESTAMP"),
            ("leads", "reply_intent", "TEXT"),
            ("leads", "auto_replies_sent", "INTEGER DEFAULT 0"),
            ("inboxes", "reply_to", "TEXT"),
            ("inboxes", "cc_email", "TEXT")
        ]:
            async with db.execute(f"PRAGMA table_info({table})") as cursor:
                columns = await cursor.fetchall()
            if col not in {column[1] for column in columns}:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type};")

        await db.execute("""
            UPDATE leads SET mailbox_tag = status,
                status = CASE WHEN reply_intent IS NOT NULL THEN 'replied' ELSE 'sent' END
            WHERE status IN ('hot', 'needs_reply', 'not_interested', 'meeting_booked')
        """)
        await db.execute("UPDATE leads SET status = 'send_review', dispatch_claim = NULL WHERE dispatch_claim IS NOT NULL")
        await db.execute("CREATE INDEX IF NOT EXISTS leads_campaign_status ON leads (campaign_id, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS leads_followups ON leads (status, next_followup_at)")
        # A discovery task dies with the server. On start, nothing can still be discovering.
        await db.execute("UPDATE campaigns SET status = 'error' WHERE status IN ('discovering', 'sending')")
        await db.commit()


async def get_db_connection() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(DB_FILE)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL;")
    await conn.execute("PRAGMA busy_timeout = 10000;")
    await conn.execute("PRAGMA foreign_keys=ON;")
    return conn

