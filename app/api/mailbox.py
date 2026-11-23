import asyncio
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, EmailStr, Field
from typing import Literal, Optional
from app.database import get_db_connection
from app.engine.sequencer import send_single_email
from app.engine.replies import booking_link

router = APIRouter(prefix="/api/mailbox", tags=["mailbox"])


class ManualReplyRequest(BaseModel):
    lead_id: int
    body: str = Field(min_length=1, max_length=20000)
    subject: Optional[str] = None
    cc_email: Optional[EmailStr] = None


class UpdateThreadTagRequest(BaseModel):
    tag: Literal["hot", "needs_reply", "not_interested", "meeting_booked", "cleared"]


@router.get("/threads")
async def list_mailbox_threads(folder: str = "all", search: Optional[str] = None):
    """
    Returns outbound and inbound email conversation threads.
    Matches the Explee / Smartlead unified mailbox architecture.
    """
    conn = await get_db_connection()
    try:
        query = """
            SELECT l.*, c.name as campaign_name, c.value_proposition
            FROM leads l
            JOIN campaigns c ON l.campaign_id = c.id
            WHERE l.status IN ('sent', 'replied', 'auto_replied', 'opted_out')
        """
        params = []
        if folder == "hot":
            query += " AND (l.mailbox_tag = 'hot' OR (l.mailbox_tag IS NULL AND l.is_hot_lead = 1))"
        elif folder == "needs_reply":
            query += " AND (l.mailbox_tag = 'needs_reply' OR (l.mailbox_tag IS NULL AND l.status = 'replied'))"

        if search:
            query += " AND (l.contact_name LIKE ? OR l.company_name LIKE ? OR l.email LIKE ?)"
            s_param = f"%{search}%"
            params.extend([s_param, s_param, s_param])

        query += " ORDER BY COALESCE(l.sent_at, l.created_at) DESC LIMIT 100"

        cur = await conn.execute(query, params)
        rows = await cur.fetchall()
        threads = []
        for r in rows:
            d = dict(r)
            threads.append({
                "id": d["id"],
                "contact_name": d.get("contact_name") or "Decision Maker",
                "job_title": d.get("job_title") or "Executive",
                "company_name": d.get("company_name") or d.get("company_domain"),
                "company_domain": d.get("company_domain"),
                "email": d.get("email"),
                "status": d.get("status"),
                "is_hot_lead": bool(d.get("is_hot_lead")),
                "tag": d.get("mailbox_tag"),
                "campaign_name": d.get("campaign_name"),
                "sent_from_inbox": d.get("sent_from_inbox") or "",
                "sent_at": d.get("sent_at") or d.get("created_at"),
                "subject": d.get("personalized_subject") or "Outreach Inquiry",
                "snippet": (d.get("personalized_body") or "")[:120].replace("\n", " "),
                "full_body": d.get("personalized_body"),
                "followup_step": d.get("followup_step") or 1,
            })
        return threads
    finally:
        await conn.close()


@router.get("/threads/{lead_id}")
async def get_thread_detail(lead_id: int):
    """
    Retrieves full conversation details, history, and suggested next nudge for a lead.
    """
    conn = await get_db_connection()
    try:
        cur = await conn.execute("""
            SELECT l.*, c.name as campaign_name, c.value_proposition, c.reply_to_email, c.cc_email, c.booking_url
            FROM leads l
            JOIN campaigns c ON l.campaign_id = c.id
            WHERE l.id = ?
        """, (lead_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Thread not found")
        lead = dict(row)

        booking_url = booking_link(lead)

        # Generate intelligent contextual nudge copy (Explee-style "Nudge Tyler?")
        first_name = lead.get("first_name") or (lead.get("contact_name", "").split()[0] if lead.get("contact_name") else "there")
        booking_prompt = (f"You can book a time here: {booking_url}" if booking_url
                          else "Would you have time for a short call?")
        suggested_nudge = (
            f"Hi {first_name},\n\n"
            f"{booking_prompt}\n\nLooking forward to speaking."
        )

        our_address = lead.get("sent_from_inbox") or ""
        cur = await conn.execute(
            "SELECT * FROM messages WHERE lead_id = ? ORDER BY created_at ASC, id ASC", (lead_id,)
        )
        messages = [
            {
                "direction": m["direction"],
                "sender": our_address if m["direction"] == "outbound" else lead.get("email"),
                "recipient": lead.get("email") if m["direction"] == "outbound" else our_address,
                "subject": m["subject"],
                "body": m["body"],
                "intent": m["intent"],
                "timestamp": m["created_at"],
            }
            for m in await cur.fetchall()
        ]
        # Emails sent before the messages table existed have only the lead row.
        if not any(m["direction"] == "outbound" for m in messages):
            messages.insert(0, {
                "direction": "outbound",
                "sender": our_address,
                "recipient": lead.get("email"),
                "subject": lead.get("personalized_subject"),
                "body": lead.get("personalized_body"),
                "timestamp": lead.get("sent_at") or lead.get("created_at"),
            })

        return {
            "lead": lead,
            "booking_url": booking_url,
            "suggested_nudge": suggested_nudge,
            "messages": messages,
        }
    finally:
        await conn.close()


@router.post("/reply")
async def send_manual_reply(req: ManualReplyRequest):
    """
    Sends a manual reply from the original sending inbox.
    """
    conn = await get_db_connection()
    try:
        cur = await conn.execute("SELECT * FROM leads WHERE id = ?", (req.lead_id,))
        lead_row = await cur.fetchone()
        if not lead_row:
            raise HTTPException(status_code=404, detail="Lead not found")
        lead = dict(lead_row)

        cur_inbox = await conn.execute("SELECT * FROM inboxes WHERE status = 'active' AND email = ? AND sent_today < daily_quota",
                                      (lead["sent_from_inbox"],))
        inbox_row = await cur_inbox.fetchone()
        if not inbox_row:
            raise HTTPException(status_code=400, detail="The original sending inbox is unavailable or has reached its quota.")
        inbox = dict(inbox_row)

        message_cursor = await conn.execute(
            """SELECT message_id FROM messages WHERE lead_id = ? AND message_id IS NOT NULL
               ORDER BY id DESC LIMIT 1""", (req.lead_id,),
        )
        latest = await message_cursor.fetchone()
        subject = req.subject or f"Re: {lead.get('personalized_subject') or 'quick question'}"
        inbox_copy = dict(inbox)
        if req.cc_email:
            inbox_copy["cc_email"] = req.cc_email

        send_res = await send_single_email(
            inbox=inbox_copy,
            to_email=lead["email"],
            subject=subject,
            body=req.body,
            lead_id=req.lead_id,
            is_reply=True,
            in_reply_to=latest["message_id"] if latest else None,
        )

        if send_res.get("success"):
            return {"status": "sent", "recipient": lead["email"], "subject": subject}
        else:
            raise HTTPException(status_code=500, detail=f"Relay error: {send_res.get('error')}")
    finally:
        await conn.close()


@router.post("/threads/{lead_id}/tag")
async def update_thread_tag(lead_id: int, req: UpdateThreadTagRequest):
    """
    Updates the prospect status tag (Hot, Replied, Not Interested, Meeting Booked).
    """
    conn = await get_db_connection()
    try:
        is_hot = 1 if req.tag == "hot" else 0
        tag = req.tag if req.tag != "cleared" else None
        cursor = await conn.execute("""
            UPDATE leads
            SET is_hot_lead = ?,
                mailbox_tag = ?
            WHERE id = ?
        """, (is_hot, tag, lead_id))
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="Thread not found")
        await conn.commit()
        return {"status": "updated", "lead_id": lead_id, "tag": req.tag}
    finally:
        await conn.close()
