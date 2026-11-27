from fastapi import APIRouter, HTTPException, BackgroundTasks
import csv
import io
import re

from pydantic import BaseModel, EmailStr, Field, HttpUrl
from typing import Literal
from typing import Optional, List
from app.database import get_db_connection
from app.engine.discovery import discover_leads_for_icp
from app.engine.verifier import verify_email_smtp
from app.engine.copywriter import generate_cold_email
from app.engine.sequencer import dispatch_campaign_batch, get_next_available_inbox
from app.engine.analyzer import analyze_my_product

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


class ProductAnalyzeRequest(BaseModel):
    url: str


class AutopilotRequest(BaseModel):
    url: str
    auto_send: Literal[False] = False
    max_campaigns: int = 3
    leads_per_campaign: int = 5


@router.post("/analyze-product")
async def analyze_product_endpoint(req: ProductAnalyzeRequest):
    try:
        return await analyze_my_product(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/autopilot")
async def launch_autopilot(req: AutopilotRequest, background_tasks: BackgroundTasks):
    """
    Explee-style 1-click autopilot:
    1. Crawls the submitted website & extracts product + ICP segments
    2. Creates campaigns for each ICP segment
    3. Discovers leads, verifies mailboxes, drafts AI copy, and optionally auto-sends
    """
    analysis = await analyze_product_endpoint(ProductAnalyzeRequest(url=req.url))
    value_prop = analysis.get("value_proposition") or f"Solutions by {req.url}"
    product_name = analysis.get("product_name", req.url)
    icps = (analysis.get("target_icps") or [])[: max(1, min(req.max_campaigns, 6))]

    created = []
    conn = await get_db_connection()
    try:
        for icp in icps:
            camp_name = icp.get("campaign_name") or "Target ICP"
            icp_query = icp.get("icp_query") or value_prop
            cursor = await conn.execute(
                """
                INSERT INTO campaigns (name, icp_prompt, target_industry, value_proposition, status)
                VALUES (?, ?, ?, ?, 'discovering')
                """,
                (camp_name, icp_query, product_name[:80], value_prop),
            )
            cid = cursor.lastrowid
            created.append({"id": cid, "name": camp_name, "icp_query": icp_query})
            background_tasks.add_task(
                run_discovery_task,
                cid,
                icp_query,
                value_prop,
                max(1, min(req.leads_per_campaign, 15)),
            )
        await conn.commit()
    finally:
        await conn.close()

    return {
        "product_name": product_name,
        "value_proposition": value_prop,
        "key_features": analysis.get("key_features", []),
        "crawled_subpages": analysis.get("crawled_subpages", []),
        "auto_send": req.auto_send,
        "campaigns_created": created,
    }


class CampaignCreate(BaseModel):
    name: str
    icp_prompt: str
    target_industry: Optional[str] = "B2B SaaS"
    value_proposition: str
    auto_send: Literal[False] = False


async def run_discovery_task(
    campaign_id: int,
    icp_prompt: str,
    value_prop: str,
    max_companies: int = 8,
):
    conn = await get_db_connection()
    try:
        cur = await conn.execute("SELECT booking_url, sender_name FROM campaigns WHERE id = ?", (campaign_id,))
        camp_row = await cur.fetchone()
        campaign_booking_url = camp_row["booking_url"] if camp_row else None
        campaign_sender_name = (camp_row["sender_name"] if camp_row else None) or ""
    finally:
        await conn.close()

    try:
        leads = await discover_leads_for_icp(icp_prompt, max_companies=max_companies)

        # A second discovery run must not add a company the campaign already holds.
        conn = await get_db_connection()
        try:
            cur = await conn.execute("SELECT company_domain FROM leads WHERE campaign_id = ?", (campaign_id,))
            known = {r["company_domain"] for r in await cur.fetchall()}
        finally:
            await conn.close()
        leads = [l for l in leads if l["company_domain"] not in known]

        enriched_leads = []
        for l in leads:
            # No status from discovery means nobody checked the mailbox: verify it, never assume.
            ver_status = l.get("verification_status") or "unverified"
            ver_reason = l.get("verification_details") or ""
            if ver_status == "unverified" and l.get("email"):
                try:
                    ver_res = await verify_email_smtp(l["email"], timeout=2.0)
                    ver_status = ver_res.get("status", "unverified")
                    ver_reason = ver_res.get("details", "")
                except Exception as ve:
                    ver_reason = str(ve)

            # Auto-draft AI personalized email
            subject = f"quick question for {l['company_name']}"
            body = f"Hi {l['contact_name'].split()[0] if l.get('contact_name') else 'there'},\n\nSaw what you are building at {l['company_name']} and wanted to connect."
            try:
                email_draft = await generate_cold_email(
                    company_name=l["company_name"],
                    company_domain=l["company_domain"],
                    contact_name=l["contact_name"],
                    job_title=l["job_title"],
                    company_summary=l.get("summary", ""),
                    value_proposition=value_prop,
                    sender_name=campaign_sender_name,
                    booking_url=campaign_booking_url,
                )
                if email_draft.get("subject"):
                    subject = email_draft["subject"]
                if email_draft.get("body"):
                    body = email_draft["body"]
            except Exception as ge:
                print(f"[Discovery] Copy generation note: {ge}")

            enriched_leads.append({
                **l,
                "verification_status": ver_status,
                "verification_reason": ver_reason,
                "personalized_subject": subject,
                "personalized_body": body,
            })

        # Save to DB in one clean transaction
        conn = await get_db_connection()
        try:
            for l in enriched_leads:
                await conn.execute("""
                    INSERT INTO leads (
                        campaign_id, company_name, company_domain, website_url,
                        contact_name, job_title, email, status,
                        verification_status, verification_reason,
                        personalized_subject, personalized_body
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'enriched', ?, ?, ?, ?)
                """, (
                    campaign_id, l["company_name"], l["company_domain"], l["website_url"],
                    l["contact_name"], l["job_title"], l["email"],
                    l["verification_status"], l["verification_reason"],
                    l["personalized_subject"], l["personalized_body"]
                ))

            await conn.execute("""
                UPDATE campaigns 
                SET total_leads = (SELECT COUNT(*) FROM leads WHERE campaign_id = ?),
                    status = 'ready'
                WHERE id = ?
            """, (campaign_id, campaign_id))
            await conn.commit()
        finally:
            await conn.close()


    except Exception as e:
        print(f"[Campaign Task Error]: {e}")
        conn = await get_db_connection()
        try:
            await conn.execute("UPDATE campaigns SET status = 'error' WHERE id = ?", (campaign_id,))
            await conn.commit()
        finally:
            await conn.close()


@router.get("")
async def list_campaigns():
    conn = await get_db_connection()
    cursor = await conn.execute("""
        SELECT c.*, 
            (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.id AND l.verification_status IN ('verified', 'deliverable')) as verified_count,
            (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.id AND l.sent_at IS NOT NULL) as sent_leads_count,
            (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.id AND l.status = 'queued' AND l.dispatch_claim IS NULL AND l.verification_status IN ('verified', 'deliverable') AND l.email IS NOT NULL AND l.email != '') as ready_leads_count
        FROM campaigns c
        ORDER BY c.created_at DESC
    """)
    rows = await cursor.fetchall()
    await conn.close()

    result = []
    for r in rows:
        d = dict(r)
        total = d.get("total_leads") or 0
        sent = d.get("sent_leads_count") or 0
        d["sent_count"] = sent
        raw_status = d.get("status", "draft")

        # Determine real-time display status
        if raw_status in ("discovering", "sending", "error"):
            d["display_status"] = raw_status
        elif total == 0:
            d["display_status"] = "no_leads"
        elif total > 0 and sent >= total:
            d["display_status"] = "completed"
        elif total > 0 and sent < total:
            d["display_status"] = "ready"
        else:
            d["display_status"] = raw_status

        result.append(d)
    return result


@router.post("")
async def create_campaign(req: CampaignCreate, background_tasks: BackgroundTasks):
    conn = await get_db_connection()
    cursor = await conn.execute("""
        INSERT INTO campaigns (name, icp_prompt, target_industry, value_proposition, status)
        VALUES (?, ?, ?, ?, 'discovering')
    """, (req.name, req.icp_prompt, req.target_industry, req.value_proposition))
    campaign_id = cursor.lastrowid
    await conn.commit()
    await conn.close()

    # Automatically trigger discovery in the background
    background_tasks.add_task(run_discovery_task, campaign_id, req.icp_prompt, req.value_proposition)

    return {"id": campaign_id, "name": req.name, "status": "discovering"}


class CampaignUpdate(BaseModel):
    """Settings an operator can change after a campaign exists. Omitted fields stay as they are."""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    value_proposition: Optional[str] = Field(None, max_length=4000)
    inbox_id: Optional[int] = Field(None, ge=1)
    reply_to_email: Optional[EmailStr] = None
    cc_email: Optional[EmailStr] = None
    booking_url: Optional[HttpUrl] = None
    sender_name: Optional[str] = Field(None, max_length=120)
    knowledge_base: Optional[str] = Field(None, max_length=20000)
    followup_1_body: Optional[str] = Field(None, max_length=5000)
    followup_2_body: Optional[str] = Field(None, max_length=5000)
    followup_1_days: Optional[int] = Field(None, ge=1, le=60)
    followup_2_days: Optional[int] = Field(None, ge=1, le=60)


@router.patch("/{campaign_id}")
async def update_campaign(campaign_id: int, req: CampaignUpdate):
    changes = req.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to change")
    for key in ("reply_to_email", "cc_email", "booking_url"):
        if changes.get(key) is not None:
            changes[key] = str(changes[key])
    conn = await get_db_connection()
    try:
        cur = await conn.execute("SELECT id FROM campaigns WHERE id = ?", (campaign_id,))
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="Campaign not found")
        if changes.get("inbox_id") is not None:
            cur = await conn.execute("SELECT id FROM inboxes WHERE id = ?", (changes["inbox_id"],))
            if not await cur.fetchone():
                raise HTTPException(status_code=400, detail="That inbox does not exist")
        # Column names come from the model above, never from the request body.
        columns = ", ".join(f"{k} = ?" for k in changes)
        await conn.execute(f"UPDATE campaigns SET {columns} WHERE id = ?", (*changes.values(), campaign_id))
        await conn.commit()
    finally:
        await conn.close()
    return {"id": campaign_id, "updated": sorted(changes)}


class LeadImport(BaseModel):
    csv: str = Field(..., max_length=5_000_000)


IMPORT_COLUMNS = (
    "email", "first_name", "last_name", "contact_name", "company_name", "company_domain",
    "website_url", "job_title", "personalized_subject", "personalized_body",
)
# Statuses a list may carry over from another verifier. Anything else is imported as unverified.
IMPORTED_STATUSES = {"verified", "deliverable", "catch_all", "risky", "invalid", "unverified"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.post("/{campaign_id}/import")
async def import_leads(campaign_id: int, req: LeadImport):
    """Adds leads from a CSV. Imported leads land as 'enriched': nothing sends until an operator queues them."""
    reader = csv.DictReader(io.StringIO(req.csv.lstrip("\ufeff")))
    if not reader.fieldnames or "email" not in [f.strip().lower() for f in reader.fieldnames]:
        raise HTTPException(status_code=400, detail="The CSV needs a header row with an email column")

    conn = await get_db_connection()
    added = skipped_duplicate = skipped_suppressed = skipped_invalid = 0
    try:
        cur = await conn.execute("SELECT id FROM campaigns WHERE id = ?", (campaign_id,))
        if not await cur.fetchone():
            raise HTTPException(status_code=404, detail="Campaign not found")
        cur = await conn.execute("SELECT LOWER(email) AS e FROM leads WHERE campaign_id = ? AND email IS NOT NULL", (campaign_id,))
        known = {r["e"] for r in await cur.fetchall()}
        cur = await conn.execute("SELECT LOWER(value) AS v FROM suppression_list")
        suppressed = {r["v"] for r in await cur.fetchall()}

        for raw in reader:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            email = row.get("email", "").lower()
            if not EMAIL_RE.match(email):
                skipped_invalid += 1
                continue
            if email in known:
                skipped_duplicate += 1
                continue
            domain = row.get("company_domain") or email.rsplit("@", 1)[-1]
            if email in suppressed or domain.lower() in suppressed:
                skipped_suppressed += 1
                continue
            status = row.get("verification_status", "").lower()
            if status not in IMPORTED_STATUSES:
                status = "unverified"
            contact = row.get("contact_name") or " ".join(
                p for p in (row.get("first_name"), row.get("last_name")) if p
            )
            values = {c: row.get(c) or None for c in IMPORT_COLUMNS}
            values.update(email=email, company_domain=domain, contact_name=contact or None)
            await conn.execute(
                f"""INSERT INTO leads (campaign_id, {", ".join(IMPORT_COLUMNS)},
                                       verification_status, verification_reason, status)
                    VALUES (?, {", ".join("?" for _ in IMPORT_COLUMNS)}, ?, ?, 'enriched')""",
                (campaign_id, *(values[c] for c in IMPORT_COLUMNS), status,
                 "Status carried over from the imported list" if status != "unverified" else None),
            )
            known.add(email)
            added += 1

        await conn.execute(
            "UPDATE campaigns SET total_leads = (SELECT COUNT(*) FROM leads WHERE campaign_id = ?) WHERE id = ?",
            (campaign_id, campaign_id),
        )
        await conn.commit()
    finally:
        await conn.close()
    return {
        "added": added,
        "skipped_duplicate": skipped_duplicate,
        "skipped_suppressed": skipped_suppressed,
        "skipped_invalid": skipped_invalid,
    }


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: int):
    conn = await get_db_connection()
    cursor = await conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
    camp = await cursor.fetchone()
    if not camp:
        await conn.close()
        raise HTTPException(status_code=404, detail="Campaign not found")

    leads_cur = await conn.execute("SELECT * FROM leads WHERE campaign_id = ? ORDER BY id DESC", (campaign_id,))
    leads = [dict(r) for r in await leads_cur.fetchall()]
    await conn.close()

    result = dict(camp)
    result["leads"] = leads
    return result


@router.post("/{campaign_id}/discover")
async def trigger_campaign_discovery(campaign_id: int, background_tasks: BackgroundTasks):
    conn = await get_db_connection()
    try:
        cursor = await conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
        camp = await cursor.fetchone()
        if not camp:
            raise HTTPException(status_code=404, detail="Campaign not found")
        camp_dict = dict(camp)
        claim = await conn.execute(
            "UPDATE campaigns SET status = 'discovering' WHERE id = ? AND status NOT IN ('sending', 'discovering')",
            (campaign_id,),
        )
        if not claim.rowcount:
            raise HTTPException(status_code=409, detail="Campaign is already running.")
        await conn.commit()
    finally:
        await conn.close()

    background_tasks.add_task(
        run_discovery_task,
        campaign_id,
        camp_dict["icp_prompt"],
        camp_dict.get("value_proposition") or ""
    )
    return {"id": campaign_id, "status": "discovering"}


async def _run_campaign_dispatch_background(campaign_id: int, limit: int = 20):
    try:
        await dispatch_campaign_batch(campaign_id, limit=limit)
    except Exception as e:
        print(f"[Sequencer Background Error]: {e}")
    finally:
        conn = await get_db_connection()
        try:
            cur = await conn.execute("SELECT COUNT(*) as tot, (SELECT COUNT(*) FROM leads WHERE campaign_id = ? AND sent_at IS NOT NULL) as snt FROM leads WHERE campaign_id = ?", (campaign_id, campaign_id))
            r = await cur.fetchone()
            tot = r["tot"] if r else 0
            snt = r["snt"] if r else 0
            new_status = "completed" if (tot > 0 and snt >= tot) else "ready"
            await conn.execute("UPDATE campaigns SET status = ?, sent_count = ? WHERE id = ?", (new_status, snt, campaign_id))
            await conn.commit()
        finally:
            await conn.close()


@router.post("/{campaign_id}/launch")
async def launch_campaign(campaign_id: int, background_tasks: BackgroundTasks):
    conn = await get_db_connection()
    try:
        cur = await conn.execute("""
            SELECT 
                (SELECT COUNT(*) FROM leads WHERE campaign_id = ? AND status = 'queued' AND dispatch_claim IS NULL AND verification_status IN ('verified', 'deliverable') AND email IS NOT NULL AND email != '') as sendable,
                (SELECT COUNT(*) FROM leads WHERE campaign_id = ?) as total,
                (SELECT COUNT(*) FROM leads WHERE campaign_id = ? AND sent_at IS NOT NULL) as sent
        """, (campaign_id, campaign_id, campaign_id))
        row = await cur.fetchone()
        sendable = row["sendable"] if row else 0
        total = row["total"] if row else 0
        sent = row["sent"] if row else 0

        camp_cur = await conn.execute("SELECT status FROM campaigns WHERE id = ?", (campaign_id,))
        camp_row = await camp_cur.fetchone()
        current_status = camp_row["status"] if camp_row else "draft"
    finally:
        await conn.close()

    if not camp_row:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if current_status == "sending":
        return {"status": "sending", "message": "Campaign is already dispatching emails in the background!"}
    if current_status == "discovering":
        return {"status": "discovering", "message": "Campaign is currently discovering leads. Please wait until discovery finishes."}

    if sendable == 0:
        if total > 0 and sent >= total:
            return {
                "dispatched": 0,
                "status": "completed",
                "message": f"All {total} leads in this campaign have already been dispatched!"
            }
        return {
            "dispatched": 0,
            "status": "no_approved_leads",
            "message": "Review and approve eligible leads before sending."
        }

    inbox = await get_next_available_inbox(campaign_id)
    if not inbox:
        conn = await get_db_connection()
        try:
            inbox_cur = await conn.execute("SELECT email, daily_quota, sent_today FROM inboxes WHERE status = 'active' LIMIT 1")
            ib_row = await inbox_cur.fetchone()
        finally:
            await conn.close()

        if ib_row:
            return {
                "status": "quota_exceeded",
                "dispatched": 0,
                "message": f"Daily sending quota reached ({ib_row['sent_today']}/{ib_row['daily_quota']} sent today for {ib_row['email']}). Increase daily quota in Inboxes & Domains or reset quota to send more."
            }
        return {
            "status": "no_inbox",
            "dispatched": 0,
            "message": "No active sending inboxes found. Please connect an inbox in Inboxes & Domains."
        }

    conn = await get_db_connection()
    try:
        claim = await conn.execute(
            "UPDATE campaigns SET status = 'sending' WHERE id = ? AND status NOT IN ('sending', 'discovering')",
            (campaign_id,),
        )
        if not claim.rowcount:
            return {"status": "busy", "message": "Campaign is already running."}
        await conn.commit()
    finally:
        await conn.close()

    background_tasks.add_task(_run_campaign_dispatch_background, campaign_id, 20)
    return {
        "status": "sending",
        "message": f"Started dispatching up to {min(sendable, 20)} approved leads.",
        "queue_count": sendable
    }


@router.delete("/{campaign_id}")
async def delete_campaign(campaign_id: int):
    conn = await get_db_connection()
    try:
        result = await conn.execute(
            """DELETE FROM campaigns WHERE id = ? AND status NOT IN ('discovering', 'sending')
               AND NOT EXISTS (SELECT 1 FROM leads WHERE campaign_id = ? AND dispatch_claim IS NOT NULL)""",
            (campaign_id, campaign_id),
        )
        if not result.rowcount:
            raise HTTPException(status_code=409, detail="Campaign is missing or has active work.")
        await conn.commit()
    finally:
        await conn.close()
    return {"ok": True}

