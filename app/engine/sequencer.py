import asyncio
import random
import ssl
from email.message import EmailMessage
from email.utils import make_msgid, formatdate
import aiosmtplib
import httpx
from urllib.parse import urlsplit
from typing import Dict, Any, Optional
from app.database import get_db_connection
from app.engine.copywriter import resolve_spintax
from app.config import settings


async def get_next_available_inbox(campaign_id: Optional[int] = None, *, sender: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Retrieves an active inbox under its daily quota. A campaign can pin its own sender inbox, Reply-To and CC."""
    conn = await get_db_connection()
    try:
        camp = None
        if campaign_id is not None:
            cur = await conn.execute(
                "SELECT inbox_id, reply_to_email, cc_email FROM campaigns WHERE id = ?", (campaign_id,))
            camp = await cur.fetchone()
        pinned = camp["inbox_id"] if camp else None
        cur = await conn.execute("""
            SELECT * FROM inboxes
            WHERE status = 'active' AND sent_today < MIN(daily_quota, ?) AND (? IS NULL OR id = ?)
              AND (? IS NULL OR email = ?)
            ORDER BY last_sent_at ASC NULLS FIRST, id ASC
            LIMIT 1
        """, (settings.max_daily_sends_per_inbox, pinned, pinned, sender, sender))
        row = await cur.fetchone()
        if row:
            inbox = dict(row)
            if camp and camp["reply_to_email"]:
                inbox["reply_to"] = camp["reply_to_email"]
            if camp and camp["cc_email"]:
                inbox["cc_email"] = camp["cc_email"]
            return inbox
        return None
    finally:
        await conn.close()


def compliance_footer() -> str:
    """Opt-out line plus postal address, added under every cold email and follow-up."""
    lines = [settings.opt_out_line.strip(), settings.postal_address.strip()]
    return "\n\n".join(line for line in lines if line)


async def _record_send_success(
    inbox_email: str, lead_id: Optional[int], is_reply: bool = False,
    subject: str = "", body: str = "", message_id: Optional[str] = None,
) -> None:
    """Record accepted delivery and advance the sequence."""
    conn = await get_db_connection()
    try:
        if lead_id:
            await conn.execute(
                "INSERT OR IGNORE INTO messages (lead_id, direction, message_id, subject, body) "
                "VALUES (?, 'outbound', ?, ?, ?)",
                (lead_id, message_id, subject, body),
            )

        # A reply in a live conversation must not put the lead back into the cold sequence.
        if lead_id and not is_reply:
            await conn.execute("""
                UPDATE leads
                SET status = CASE WHEN status IN ('queued', 'sent') THEN 'sent' ELSE status END,
                    sent_at = COALESCE(sent_at, CURRENT_TIMESTAMP),
                    dispatch_claim = NULL,
                    sent_from_inbox = ?,
                    followup_step = COALESCE(followup_step, 0) + 1,
                    next_followup_at = CASE
                        WHEN status NOT IN ('queued', 'sent') THEN NULL
                        WHEN COALESCE(followup_step, 0) = 0 THEN datetime('now', '+' || COALESCE(
                            (SELECT followup_1_days FROM campaigns WHERE id = leads.campaign_id), ?) || ' days')
                        WHEN COALESCE(followup_step, 0) = 1 THEN datetime('now', '+' || COALESCE(
                            (SELECT followup_2_days FROM campaigns WHERE id = leads.campaign_id), ?) || ' days')
                        ELSE NULL
                    END
                WHERE id = ?
            """, (inbox_email, settings.followup_1_days, settings.followup_2_days, lead_id))

            await conn.execute("""
                UPDATE campaigns
                SET sent_count = (SELECT COUNT(*) FROM leads WHERE campaign_id = campaigns.id AND sent_at IS NOT NULL)
                WHERE id = (SELECT campaign_id FROM leads WHERE id = ?)
            """, (lead_id,))

        await conn.commit()
    finally:
        await conn.close()


def provider_base_url(value: str) -> str:
    base = value.rstrip("/")
    if "://" not in base:
        base = f"https://{base}"
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Mail relay API requires an HTTPS base URL without credentials, query, or fragment.")
    return base


async def _send_via_cf_worker(
    inbox: Dict[str, Any], to_email: str, subject: str, body: str
) -> None:
    """Dispatches email via the OpenExplee Cloudflare Email Worker (/send)."""
    base_url = provider_base_url(inbox["smtp_host"])

    payload: Dict[str, Any] = {
        "from": inbox["email"],
        "to": to_email,
        "subject": subject,
        "text": body,
    }
    if inbox.get("display_name"):
        payload["fromName"] = inbox["display_name"]
    if inbox.get("cc_email"):
        payload["cc"] = inbox["cc_email"]
    if inbox.get("reply_to"):
        payload["replyTo"] = inbox["reply_to"]

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{base_url}/send",
            headers={"Authorization": f"Bearer {inbox['smtp_password']}"},
            json=payload,
        )
        if not 200 <= resp.status_code < 300:
            raise RuntimeError(f"CF Worker HTTP {resp.status_code}")


async def _send_via_mailflare(
    inbox: Dict[str, Any], to_email: str, subject: str, body: str
) -> None:
    """Dispatches email via Mailflare's authenticated REST API (/api/v1/send)."""
    base_url = provider_base_url(inbox["smtp_host"])

    from_addr = f"{inbox['display_name']} <{inbox['email']}>" if inbox.get("display_name") else inbox["email"]
    payload: Dict[str, Any] = {
        "from": from_addr,
        "to": [to_email],
        "subject": subject,
        "text": body,
        "mailboxId": inbox["smtp_user"],
    }
    if inbox.get("cc_email"):
        payload["cc"] = inbox["cc_email"]

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{base_url}/api/v1/send",
            headers={"Authorization": f"Bearer {inbox['smtp_password']}"},
            json=payload,
        )
        if not 200 <= resp.status_code < 300:
            raise RuntimeError(f"Mailflare HTTP {resp.status_code}")


async def _send_via_smtp(
    inbox: Dict[str, Any], to_email: str, subject: str, body: str,
    in_reply_to: Optional[str] = None,
) -> str:
    """Dispatches email over standard SMTP (Google Workspace, M365, Hyvor Relay, Custom)."""
    msg = EmailMessage()
    from_header = f"{inbox['display_name']} <{inbox['email']}>" if inbox.get("display_name") else inbox["email"]
    msg["From"] = from_header
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=inbox["email"].split("@")[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    if inbox.get("reply_to"):
        msg["Reply-To"] = inbox["reply_to"]
    if inbox.get("cc_email"):
        msg["Cc"] = inbox["cc_email"]

    unsubscribe_mail = f"mailto:unsubscribe@{inbox['email'].split('@')[-1]}?subject=Unsubscribe"
    msg["List-Unsubscribe"] = f"<{unsubscribe_mail}>"
    msg.set_content(body)

    port = inbox.get("smtp_port", 587)
    use_tls = (port == 465)
    start_tls = not use_tls

    tls_context = ssl.create_default_context()

    smtp_client = aiosmtplib.SMTP(
        hostname=inbox["smtp_host"],
        port=port,
        use_tls=use_tls,
        start_tls=start_tls,
        tls_context=tls_context,
        timeout=15.0
    )
    try:
        await smtp_client.connect()
        if inbox.get("smtp_user") and inbox["smtp_user"] != "none" and inbox.get("smtp_password") and inbox["smtp_password"] != "none":
            await smtp_client.login(inbox["smtp_user"], inbox["smtp_password"])
        await smtp_client.send_message(msg)
    finally:
        smtp_client.close()
    return msg["Message-ID"]


async def _is_suppressed(conn, email: str) -> bool:
    clean_email = email.strip().lower()
    domain = clean_email.rsplit("@", 1)[-1]
    cursor = await conn.execute(
        "SELECT 1 FROM suppression_list WHERE LOWER(value) IN (?, ?) LIMIT 1",
        (clean_email, domain),
    )
    return await cursor.fetchone() is not None


async def _auto_suppress_bounce(email: str, lead_id: Optional[int], reason: str) -> None:
    """Adds hard-bounced addresses to the suppression list so future sends never hit them."""
    clean_email = email.strip().lower()
    conn = await get_db_connection()
    try:
        await conn.execute(
            "INSERT OR IGNORE INTO suppression_list (entry_type, value, reason) VALUES ('email', ?, ?)",
            (clean_email, reason[:200]),
        )
        if lead_id:
            await conn.execute("UPDATE leads SET status = 'bounced' WHERE id = ?", (lead_id,))
        await conn.commit()
    finally:
        await conn.close()


async def send_single_email(
    inbox: Dict[str, Any],
    to_email: str,
    subject: str,
    body: str,
    lead_id: Optional[int] = None,
    is_reply: bool = False,
    in_reply_to: Optional[str] = None,
    followup_step: int = 0,
) -> Dict[str, Any]:
    """
    Sends an outbound email via SMTP, Cloudflare Email Worker, or Mailflare API.
    Enforces suppression checks to keep domain bounce rates near 0%.
    Cold emails and follow-ups get the compliance footer; replies do not.
    """

    clean_subject = resolve_spintax(subject)
    clean_body = resolve_spintax(body)
    footer = "" if is_reply else compliance_footer()
    if footer and footer not in clean_body:
        clean_body = f"{clean_body.rstrip()}\n\n{footer}"
    result = {"success": False, "inbox_email": inbox["email"], "error": None}

    conn = await get_db_connection()
    try:
        await conn.execute("BEGIN IMMEDIATE")
        if await _is_suppressed(conn, to_email):
            return {**result, "error": "Suppressed recipient or domain."}
        if lead_id and not is_reply:
            lead_claim = await conn.execute(
                """UPDATE leads SET dispatch_claim = ?
                   WHERE id = ? AND dispatch_claim IS NULL AND COALESCE(followup_step, 0) = ?
                     AND status = ? AND email = ?
                     AND verification_status IN ('verified', 'deliverable')
                     AND (? = 0 OR (is_hot_lead = 0 AND mailbox_tag IS NULL AND next_followup_at <= CURRENT_TIMESTAMP))""",
                (followup_step, lead_id, followup_step, "sent" if followup_step else "queued", to_email, followup_step),
            )
            if not lead_claim.rowcount:
                return {**result, "error": "Lead is no longer eligible for this send."}
        quota_claim = await conn.execute(
            """UPDATE inboxes SET sent_today = sent_today + 1, last_sent_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = 'active' AND sent_today < MIN(daily_quota, ?)""",
            (inbox["id"], settings.max_daily_sends_per_inbox),
        )
        if not quota_claim.rowcount:
            return {**result, "error": "Inbox unavailable or quota reached."}
        await conn.commit()
    finally:
        await conn.close()

    try:
        provider = inbox.get("provider", "custom")
        message_id = None
        if provider == "cf_worker":
            await _send_via_cf_worker(inbox, to_email, clean_subject, clean_body)
        elif provider == "mailflare":
            await _send_via_mailflare(inbox, to_email, clean_subject, clean_body)
        else:
            message_id = await _send_via_smtp(inbox, to_email, clean_subject, clean_body, in_reply_to)

        await _record_send_success(
            inbox["email"], lead_id, is_reply=is_reply,
            subject=clean_subject, body=clean_body, message_id=message_id,
        )
        result["success"] = True

    except Exception as e:
        err_str = str(e)
        result["error"] = err_str
        if isinstance(e, aiosmtplib.SMTPRecipientsRefused):
            for recipient in e.recipients:
                if recipient.recipient.lower() == to_email.lower() and recipient.code in (550, 551, 553):
                    await _auto_suppress_bounce(to_email, lead_id, f"Recipient rejected with SMTP {recipient.code}")
        if lead_id and not is_reply:
            conn = await get_db_connection()
            try:
                await conn.execute(
                    """UPDATE leads SET dispatch_claim = NULL,
                       status = CASE WHEN status IN ('queued', 'sent') THEN 'send_review' ELSE status END
                       WHERE id = ?""", (lead_id,),
                )
                await conn.commit()
            finally:
                await conn.close()
        print(f"[Sequencer] Send failed: {type(e).__name__}")

    return result


async def dispatch_campaign_batch(campaign_id: int, limit: int = 10) -> Dict[str, Any]:
    """
    Sends queued and verified leads for a given campaign using rotating inboxes.
    Blocks invalid emails automatically to protect sender domain reputation.
    """
    conn = await get_db_connection()
    try:
        cur = await conn.execute("""
            SELECT * FROM leads
            WHERE campaign_id = ?
              AND status = 'queued' AND dispatch_claim IS NULL
              AND verification_status IN ('verified', 'deliverable')
              AND email IS NOT NULL AND email != ''
            LIMIT ?
        """, (campaign_id, max(1, min(limit, 100))))
        leads = [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()

    dispatched = 0
    errors = []

    if not leads:
        conn = await get_db_connection()
        try:
            cur_all = await conn.execute("SELECT COUNT(*) AS c FROM leads WHERE campaign_id = ?", (campaign_id,))
            row_all = await cur_all.fetchone()
            total_in_camp = row_all["c"] if row_all else 0

            cur_sent = await conn.execute("SELECT COUNT(*) AS c FROM leads WHERE campaign_id = ? AND status = 'sent'", (campaign_id,))
            row_sent = await cur_sent.fetchone()
            sent_leads = row_sent["c"] if row_sent else 0
        finally:
            await conn.close()

        if total_in_camp > 0 and sent_leads >= total_in_camp:
            return {
                "dispatched": 0,
                "errors": [f"All {total_in_camp} leads in this campaign have already been dispatched! Click 'Find Leads' to discover more."]
            }
        return {
            "dispatched": 0,
            "errors": ["No queued or verified leads in this campaign yet. Click 'Find Leads' first to discover prospects."]
        }

    for lead in leads:
        inbox = await get_next_available_inbox(campaign_id)
        if not inbox:
            errors.append("No active inboxes available with remaining daily quota.")
            break

        subj = lead.get("personalized_subject") or f"quick question for {lead.get('company_name', 'you')}"
        body = lead.get("personalized_body") or f"Hi,\n\nSaw what you are building and wanted to connect."

        send_res = await send_single_email(
            inbox=inbox,
            to_email=lead["email"],
            subject=subj,
            body=body,
            lead_id=lead["id"]
        )

        if send_res["success"]:
            dispatched += 1
            # Wait the configured gap between sends. It was capped at 5 seconds, which sent a
            # whole batch in about a minute and tripped the host's outbound mail limit.
            # ponytail: a caller that awaits this inline (the MCP tool) blocks for the whole
            # batch. Upgrade: run that path as a background task like the launch endpoint.
            if lead is not leads[-1]:
                await asyncio.sleep(random.randint(*sorted((settings.min_delay_seconds, settings.max_delay_seconds))))
        else:
            errors.append(f"Failed to send to {lead['email']}: {send_res.get('error')}")

    return {"dispatched": dispatched, "errors": errors}

