from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field
from secrets import compare_digest
from typing import Optional
from app.config import settings
from app.engine.cloudflare import list_cloudflare_zones, auto_setup_cloudflare_domain
from app.engine.replies import bounced_recipients, handle_bounces, handle_inbound, parse_raw

router = APIRouter(prefix="/api/cloudflare", tags=["cloudflare"])


class InboundWebhookPayload(BaseModel):
    from_email: str
    to_email: str
    subject: Optional[str] = ""
    text: Optional[str] = ""
    message_id: Optional[str] = None
    raw: Optional[str] = Field(None, max_length=262144)  # full RFC 822 message from the Worker


@router.post("/inbound-webhook")
async def handle_inbound_reply(payload: InboundWebhookPayload, authorization: Optional[str] = Header(None)):
    """Receives inbound replies from the Cloudflare Worker: opt-outs, hot leads, auto-answers."""
    token = settings.inbound_webhook_token
    # This path is reachable from the internet, so no token configured means closed, not open.
    if not token or not compare_digest((authorization or "").encode(), f"Bearer {token}".encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if payload.raw:
        bounced = bounced_recipients(payload.raw.encode("utf-8", "replace"))
        if bounced:
            return await handle_bounces(bounced)
        # Header From is the person; the envelope sender can be a bounce address. Envelope To tells opt-out mail apart.
        return await handle_inbound(**{**parse_raw(payload.raw.encode("utf-8", "replace")), "to_email": payload.to_email})
    return await handle_inbound(
        payload.from_email, payload.subject or "", payload.text or "",
        message_id=payload.message_id, to_email=payload.to_email,
    )


class CloudflareAuthRequest(BaseModel):
    api_token: str


class CloudflareSetupRequest(BaseModel):
    api_token: str
    zone_id: str = Field(pattern=r"^[0-9a-fA-F]{32}$")
    domain_name: str
    forward_to_email: EmailStr


@router.post("/zones")
async def get_zones(req: CloudflareAuthRequest):
    zones = await list_cloudflare_zones(req.api_token)
    if not zones:
        raise HTTPException(status_code=400, detail="Invalid Cloudflare API token or no domains found")
    return zones


@router.post("/setup")
async def setup_domain(req: CloudflareSetupRequest):
    result = await auto_setup_cloudflare_domain(
        api_token=req.api_token,
        zone_id=req.zone_id,
        domain_name=req.domain_name,
        forward_to_email=req.forward_to_email,
    )
    return result

