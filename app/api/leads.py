import csv
import io
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr, Field
from typing import Literal, Optional
from app.database import get_db_connection
from app.engine.verifier import verify_email_smtp, enrich_person_email
from app.engine.copywriter import generate_cold_email
from app.engine.scraper import scrape_company_website

router = APIRouter(prefix="/api/leads", tags=["leads"])


class VerifyEmailRequest(BaseModel):
    email: str


class ScrapeUrlRequest(BaseModel):
    url: str
    max_depth: Optional[int] = 1


class EnrichEmailRequest(BaseModel):
    first_name: str
    last_name: str
    company_domain: str


class SuppressionCreateRequest(BaseModel):
    entry_type: str = "email"  # 'email' or 'domain'
    value: str
    reason: Optional[str] = "manual"


class GenerateCopyRequest(BaseModel):
    company_name: str
    company_domain: str
    contact_name: str
    job_title: str
    company_summary: Optional[str] = ""
    value_proposition: str


class UpdateLeadRequest(BaseModel):
    email: Optional[EmailStr] = None
    contact_name: Optional[str] = None
    personalized_subject: Optional[str] = Field(None, max_length=300, pattern=r"^[^\r\n]*$")
    personalized_body: Optional[str] = Field(None, max_length=20000)
    status: Optional[Literal["queued", "enriched"]] = None


@router.get("")
async def list_leads(campaign_id: Optional[int] = None, limit: int = Query(50, ge=1, le=200)):
    conn = await get_db_connection()
    if campaign_id:
        cursor = await conn.execute("SELECT * FROM leads WHERE campaign_id = ? ORDER BY id DESC LIMIT ?", (campaign_id, limit))
    else:
        cursor = await conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,))
    rows = await cursor.fetchall()
    await conn.close()
    return [dict(r) for r in rows]


@router.post("/verify")
async def verify_email_endpoint(req: VerifyEmailRequest):
    return await verify_email_smtp(req.email)


@router.post("/scrape")
async def scrape_url_endpoint(req: ScrapeUrlRequest):
    """
    Fetches bounded public HTML.
    Extracts clean markdown, metadata, headlines, emails, and subpages.
    """
    if not req.url or not req.url.strip():
        raise HTTPException(status_code=400, detail="URL is required")
    return await scrape_company_website(req.url.strip(), max_depth=req.max_depth if req.max_depth is not None else 1)


@router.post("/enrich-email")
async def enrich_email_endpoint(req: EnrichEmailRequest):
    return await enrich_person_email(req.first_name, req.last_name, req.company_domain)


@router.get("/suppression")
async def list_suppression():
    conn = await get_db_connection()
    cur = await conn.execute("SELECT * FROM suppression_list ORDER BY id DESC")
    rows = [dict(r) for r in await cur.fetchall()]
    await conn.close()
    return rows


@router.post("/suppression")
async def add_suppression(req: SuppressionCreateRequest):
    val = req.value.strip().lower().lstrip("@")
    if not val:
        raise HTTPException(status_code=400, detail="Value cannot be empty")
    conn = await get_db_connection()
    await conn.execute(
        "INSERT OR IGNORE INTO suppression_list (entry_type, value, reason) VALUES (?, ?, ?)",
        (req.entry_type, val, req.reason or "manual"),
    )
    await conn.commit()
    await conn.close()
    return {"ok": True, "entry_type": req.entry_type, "value": val}


@router.delete("/suppression/{entry_id}")
async def remove_suppression(entry_id: int):
    conn = await get_db_connection()
    await conn.execute("DELETE FROM suppression_list WHERE id = ?", (entry_id,))
    await conn.commit()
    await conn.close()
    return {"ok": True}


@router.post("/generate-copy")
async def generate_copy_endpoint(req: GenerateCopyRequest):
    return await generate_cold_email(
        company_name=req.company_name,
        company_domain=req.company_domain,
        contact_name=req.contact_name,
        job_title=req.job_title,
        company_summary=req.company_summary or "",
        value_proposition=req.value_proposition
    )


@router.get("/export.csv")
async def export_leads(campaign_id: Optional[int] = None):
    columns = ("company_name", "company_domain", "website_url", "contact_name", "job_title",
               "email", "verification_status", "status")
    conn = await get_db_connection()
    try:
        cursor = await conn.execute(
            f"SELECT {', '.join(columns)} FROM leads WHERE (? IS NULL OR campaign_id = ?) ORDER BY id",
            (campaign_id, campaign_id),
        )
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(columns)
        async for row in cursor:
            values = [str(value) if value is not None else "" for value in row]
            writer.writerow([
                "'" + value if value.lstrip().startswith(("=", "+", "-", "@"))
                or value.startswith(("\t", "\r", "\n")) else value
                for value in values
            ])
        return Response(output.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="openexplee-leads.csv"'})
    finally:
        await conn.close()


@router.post("/{lead_id}/verify")
async def verify_saved_lead(lead_id: int):
    conn = await get_db_connection()
    try:
        cursor = await conn.execute("SELECT email FROM leads WHERE id = ?", (lead_id,))
        lead = await cursor.fetchone()
        if not lead or not lead["email"]:
            raise HTTPException(status_code=404, detail="Lead has no email.")
        result = await verify_email_smtp(lead["email"])
        update = await conn.execute(
            """UPDATE leads SET verification_status = ?, verification_reason = ?,
               status = CASE WHEN status = 'queued' AND ? NOT IN ('verified', 'deliverable')
                             THEN 'enriched' ELSE status END
               WHERE id = ? AND email = ? AND dispatch_claim IS NULL""",
            (result["status"], result["details"], result["status"], lead_id, lead["email"]),
        )
        if not update.rowcount:
            raise HTTPException(status_code=409, detail="Lead changed during verification.")
        await conn.commit()
        return result
    finally:
        await conn.close()


@router.put("/{lead_id}")
async def update_lead(lead_id: int, req: UpdateLeadRequest):
    changes = req.model_dump(exclude_none=True)
    conn = await get_db_connection()
    try:
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))
        lead = await cursor.fetchone()
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        if lead["dispatch_claim"] is not None or lead["status"] not in {"discovered", "enriched", "queued", "send_review"}:
            raise HTTPException(status_code=409, detail="Lead cannot be edited in its current state.")
        if "email" in changes and changes["email"] != lead["email"]:
            changes["verification_status"] = "unverified"
            changes["verification_reason"] = "Address changed after verification."
        if changes.get("status") == "queued":
            if (changes.get("verification_status", lead["verification_status"]) not in {"verified", "deliverable"}
                    or not changes.get("email", lead["email"])
                    or not changes.get("personalized_subject", lead["personalized_subject"])
                    or not changes.get("personalized_body", lead["personalized_body"])
                    or lead["sent_at"]):
                raise HTTPException(status_code=409, detail="Queue a verified lead with a reviewed subject and message.")
        elif changes:
            changes["status"] = "enriched" if lead["status"] != "send_review" else "send_review"
        if changes:
            assignments = ", ".join(f"{column} = ?" for column in changes)
            await conn.execute(f"UPDATE leads SET {assignments} WHERE id = ?", (*changes.values(), lead_id))
        await conn.commit()
        return {"ok": True}
    finally:
        await conn.close()


@router.delete("/{lead_id}")
async def delete_lead(lead_id: int):
    conn = await get_db_connection()
    try:
        cursor = await conn.execute("DELETE FROM leads WHERE id = ? AND dispatch_claim IS NULL", (lead_id,))
        if not cursor.rowcount:
            raise HTTPException(status_code=409, detail="Lead is missing or is being sent.")
        await conn.commit()
        return {"ok": True}
    finally:
        await conn.close()
