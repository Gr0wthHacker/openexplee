from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    host: str = "127.0.0.1"
    admin_password: SecretStr = Field(default=SecretStr(""), validation_alias="OPENEXPLEE_ADMIN_PASSWORD")
    port: int = 8000
    database_path: str = "data/openexplee.db"

    # LLM settings
    default_llm_provider: str = Field(
        default="gemini",
        description="gemini | openai | anthropic | groq | ollama",
    )
    default_llm_model: str = "gemini-2.5-flash"
    gemini_api_key: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    groq_api_key: Optional[str] = None
    ollama_base_url: str = "http://localhost:11434"

    # Booking Link (Self-Hosted Cal.com on VPS)
    booking_url: str = ""

    # Search & Enrichment
    serper_api_key: Optional[str] = None

    # Sending & Deliverability Safeguards
    protected_primary_domains: str = ""
    max_daily_sends_per_inbox: int = Field(35, ge=1, le=1000)
    min_delay_seconds: int = Field(120, ge=0, le=3600)
    max_delay_seconds: int = Field(300, ge=0, le=3600)
    # Compliance footer, added under every cold email and follow-up (CAN-SPAM)
    opt_out_line: str = 'Not interested? Reply "no thanks" and I won\'t email again.'
    postal_address: str = ""

    # Reply handling: read replies over IMAP from the mailbox that Reply-To points at
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    followups_enabled: bool = False
    # Days after the first email and after follow-up 1. A campaign can override both.
    followup_1_days: int = Field(3, ge=1, le=60)
    followup_2_days: int = Field(4, ge=1, le=60)
    auto_reply_enabled: bool = False
    max_auto_replies_per_lead: int = 2
    inbound_webhook_token: str = ""


settings = Settings()

