import asyncio
from string import Template
from app.config import settings
from app.database import get_db_connection
from app.engine.sequencer import get_next_available_inbox, send_single_email
from app.engine.replies import booking_link, poll_replies


async def reset_daily_inbox_quotas() -> int:
    """Resets sent_today to 0 for inboxes whose last send was prior to today (UTC)."""
    conn = await get_db_connection()
    try:
        cur = await conn.execute("""
            UPDATE inboxes
            SET sent_today = 0
            WHERE sent_today > 0
              AND (last_sent_at IS NULL OR DATE(last_sent_at) < DATE('now'))
        """)
        await conn.commit()
        return cur.rowcount
    finally:
        await conn.close()


DEFAULT_FOLLOWUP_1 = (
    "{Hi|Hey} $first_name,\n\n"
    "Just bumping this to the top of your inbox in case it got buried.$booking_line"
)
DEFAULT_FOLLOWUP_2 = (
    "Hi $first_name,\n\n"
    "Haven't heard back, so I'll assume now isn't the right time and won't follow up again.\n\n"
    "Feel free to reply anytime if this becomes a priority."
)


def followup_body(lead: dict, first_name: str) -> str:
    """Follow-up text for the lead's next step. A campaign's own template wins over the default.
    Templates take $first_name, $company, $booking_link and $booking_line; {a|b} spintax still works."""
    link = booking_link(lead)
    booking_line = (
        f"\n\nHappy to jump on a quick 15-min call. Pick any slot here: {link}" if link
        else "\n\nOpen to a quick 5-minute chat this week?"
    )
    step_one = lead.get("followup_step") == 1
    template = (lead.get("followup_1_body") if step_one else lead.get("followup_2_body")) or (
        DEFAULT_FOLLOWUP_1 if step_one else DEFAULT_FOLLOWUP_2
    )
    return Template(template).safe_substitute(
        first_name=first_name,
        company=lead.get("company_name") or "your team",
        booking_link=link or "",
        booking_line=booking_line,
    )


async def process_due_followups(limit: int = 10) -> int:
    """Sends scheduled Day-3 / Day-7 follow-ups for leads that have not replied yet."""
    conn = await get_db_connection()
    try:
        cur = await conn.execute("""
            SELECT l.*, c.booking_url, c.followup_1_body, c.followup_2_body FROM leads l
            LEFT JOIN campaigns c ON c.id = l.campaign_id
            WHERE l.status = 'sent'
              AND l.is_hot_lead = 0
              AND l.mailbox_tag IS NULL
              AND l.dispatch_claim IS NULL
              AND l.followup_step IN (1, 2)
              AND l.next_followup_at IS NOT NULL
              AND l.next_followup_at <= CURRENT_TIMESTAMP
              AND l.verification_status IN ('verified', 'deliverable')
              AND l.sent_from_inbox IS NOT NULL
              AND l.email IS NOT NULL AND l.email != ''
            LIMIT ?
        """, (limit,))
        leads = [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()

    sent_count = 0
    for lead in leads:
        inbox = await get_next_available_inbox(lead.get("campaign_id"), sender=lead["sent_from_inbox"])
        if not inbox:
            continue

        orig_subj = lead.get("personalized_subject") or f"quick question for {lead.get('company_name', 'you')}"
        subj = orig_subj if orig_subj.lower().startswith("re:") else f"Re: {orig_subj}"
        first_name = (lead.get("contact_name") or "there").split()[0]

        body = followup_body(lead, first_name)

        res = await send_single_email(inbox, lead["email"], subj, body, lead_id=lead["id"], followup_step=lead["followup_step"])
        if res["success"]:
            sent_count += 1

    return sent_count


async def run_scheduler_loop(interval_seconds: int = 300) -> None:
    """Background loop for daily quota resets and automated follow-up sequences."""
    while True:
        try:
            await reset_daily_inbox_quotas()
            # Replies first: someone who answered or opted out must not get a follow-up.
            try:
                await poll_replies()
            except Exception as exc:
                # ponytail: if the mailbox can't be read we can't know who replied, so
                # hold follow-ups this tick. Upgrade: alert the owner after N failed ticks.
                print(f"[Scheduler] Reply check failed, follow-ups held this tick: {exc}")
                await asyncio.sleep(interval_seconds)
                continue
            if settings.followups_enabled and settings.imap_host and settings.imap_user and settings.imap_password:
                await process_due_followups(limit=5)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"[Scheduler] Background tick error: {exc}")
        await asyncio.sleep(interval_seconds)

