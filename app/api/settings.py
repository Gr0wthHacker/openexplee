from fastapi import APIRouter
from pydantic import BaseModel, Field, HttpUrl, field_validator
from typing import Literal, Optional
from app.config import settings
from app.database import get_db_connection

router = APIRouter(prefix="/api/settings", tags=["settings"])

PERSISTED_FIELDS = (
    "default_llm_provider",
    "default_llm_model",
    "gemini_api_key",
    "openai_api_key",
    "anthropic_api_key",
    "groq_api_key",
    "ollama_base_url",
    "booking_url",
    "serper_api_key",
    "max_daily_sends_per_inbox",
    "opt_out_line",
    "postal_address",
    "imap_host",
    "imap_port",
    "imap_user",
    "imap_password",
)


class SettingsUpdate(BaseModel):
    opt_out_line: Optional[str] = None
    postal_address: Optional[str] = None
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    imap_user: Optional[str] = None
    imap_password: Optional[str] = None
    default_llm_provider: Optional[Literal["gemini", "openai", "anthropic", "groq", "ollama"]] = None
    default_llm_model: Optional[str] = None
    gemini_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    ollama_base_url: Optional[str] = None
    booking_url: Optional[str] = None
    serper_api_key: Optional[str] = None
    max_daily_sends_per_inbox: Optional[int] = Field(None, ge=1, le=1000)

    @field_validator("booking_url")
    @classmethod
    def valid_booking_url(cls, value):
        if value:
            return str(HttpUrl(value))
        return value


def mask_key(k: Optional[str]) -> str:
    if not k:
        return ""
    return "••••••••"


async def load_persisted_settings() -> None:
    """Restores saved settings from SQLite into the in-memory settings object on boot."""
    conn = await get_db_connection()
    try:
        cur = await conn.execute("SELECT key, value FROM app_settings")
        rows = await cur.fetchall()
        for row in rows:
            k, v = row["key"], row["value"]
            if k == "default_llm_provider" and v not in {"gemini", "openai", "anthropic", "groq", "ollama"}:
                continue
            if k == "booking_url" and v == "https://cal.viktron.ai/vikash/15min":
                continue
            if k not in PERSISTED_FIELDS:
                continue
            current = getattr(settings, k)
            if isinstance(current, bool):
                setattr(settings, k, v == "True")
            elif isinstance(current, int):
                setattr(settings, k, int(v))
            else:
                setattr(settings, k, v)
    finally:
        await conn.close()


@router.get("")
async def get_settings():
    return {
        "default_llm_provider": settings.default_llm_provider,
        "default_llm_model": settings.default_llm_model,
        "gemini_api_key": mask_key(settings.gemini_api_key),
        "openai_api_key": mask_key(settings.openai_api_key),
        "anthropic_api_key": mask_key(settings.anthropic_api_key),
        "groq_api_key": mask_key(settings.groq_api_key),
        "ollama_base_url": settings.ollama_base_url,
        "booking_url": settings.booking_url,
        "serper_api_key": mask_key(settings.serper_api_key),
        "max_daily_sends_per_inbox": settings.max_daily_sends_per_inbox,
        "opt_out_line": settings.opt_out_line,
        "postal_address": settings.postal_address,
        "imap_host": settings.imap_host,
        "imap_port": settings.imap_port,
        "imap_user": settings.imap_user,
        "imap_password": mask_key(settings.imap_password),
        "auto_reply_enabled": settings.auto_reply_enabled,
    }


@router.post("")
async def update_settings(req: SettingsUpdate):
    updates = {}
    for field in PERSISTED_FIELDS:
        val = getattr(req, field)
        if val is None:
            continue
        if isinstance(val, str) and "••" in val:
            continue
        updates[field] = val

    if updates:
        conn = await get_db_connection()
        try:
            for k, v in updates.items():
                await conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (k, str(v)),
                )
            await conn.commit()
            for k, v in updates.items():
                setattr(settings, k, v)
        finally:
            await conn.close()

    return {"ok": True, "provider": settings.default_llm_provider}
