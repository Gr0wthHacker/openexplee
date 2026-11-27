import asyncio
import csv
import io
import ssl
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import database
from app.config import settings
from app.engine import scraper, sequencer
from app.main import app


PASSWORD = "test-operator-password-only"
AUTH = ("admin", PASSWORD)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", tmp_path / "test.db")
    monkeypatch.setattr(settings, "admin_password", SecretStr(PASSWORD))
    monkeypatch.setattr(settings, "inbound_webhook_token", "test-webhook-token")
    monkeypatch.setattr(settings, "imap_host", "")
    with TestClient(app) as client:
        yield client


async def insert_lead(status="enriched", company="Example Co"):
    conn = await database.get_db_connection()
    try:
        cursor = await conn.execute("INSERT INTO campaigns (name, icp_prompt) VALUES ('Test', 'Test')")
        await conn.execute(
            """INSERT INTO leads (campaign_id, company_name, email, personalized_subject,
                                  personalized_body, verification_status, status)
               VALUES (?, ?, 'person@example.com', 'Intro', 'Hello', 'verified', ?)""",
            (cursor.lastrowid, company, status),
        )
        await conn.execute(
            """INSERT INTO inboxes (email, provider, smtp_host, smtp_user, smtp_password, daily_quota)
               VALUES ('sender@example.com', 'custom', 'smtp.example.com', 'sender', 'test', 1)"""
        )
        await conn.commit()
    finally:
        await conn.close()


def test_authentication_and_webhook_gate(client, monkeypatch):
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 401
    assert client.get("/api/inboxes").status_code == 401
    assert client.get("/", auth=AUTH).status_code == 200
    assert client.get("/static/dashboard.css", auth=AUTH).status_code == 200
    assert client.post("/api/cli/run-command", auth=AUTH).status_code == 404
    assert client.post("/api/settings/clear-demo", auth=AUTH).status_code == 404
    assert client.post("/api/settings", auth=AUTH, headers={"Origin": "https://elsewhere.example"}, json={}).status_code == 403
    body = {"from_email": "a@example.com", "to_email": "b@example.com"}
    route = "/api/cloudflare/inbound-webhook"
    assert client.post(route, json=body).status_code == 401
    assert client.post(route, json=body, headers={"Authorization": "Bearer test-webhook-token"}).status_code == 200
    monkeypatch.setattr(settings, "admin_password", SecretStr(""))
    assert client.get("/", auth=AUTH).status_code == 503


def test_approval_and_csv(client):
    asyncio.run(insert_lead(company=" =FORMULA()"))
    assert client.put("/api/leads/1", auth=AUTH, json={"status": "queued"}).status_code == 200
    assert client.get("/api/campaigns", auth=AUTH).json()[0]["ready_leads_count"] == 1
    assert client.put("/api/leads/1", auth=AUTH, json={"personalized_body": "Edited"}).status_code == 200
    assert client.get("/api/campaigns", auth=AUTH).json()[0]["ready_leads_count"] == 0
    export = client.get("/api/leads/export.csv?campaign_id=1", auth=AUTH)
    assert list(csv.DictReader(io.StringIO(export.text)))[0]["company_name"] == "' =FORMULA()"
    assert client.post("/api/campaigns", auth=AUTH, json={
        "name": "Test", "icp_prompt": "Test", "value_proposition": "Test", "auto_send": True,
    }).status_code == 422


def test_mailbox_tags_preserve_status(client):
    asyncio.run(insert_lead(status="replied"))
    response = client.post("/api/mailbox/threads/1/tag", auth=AUTH, json={"tag": "hot"})
    assert response.status_code == 200
    assert client.get("/api/mailbox/threads?folder=hot", auth=AUTH).json()[0]["status"] == "replied"
    assert client.get("/api/mailbox/threads", auth=AUTH).json()[0]["tag"] == "hot"


def test_claim_prevents_duplicate_and_enforces_quota(client):
    async def check():
        await insert_lead(status="queued")
        inbox = await sequencer.get_next_available_inbox(1)
        with patch.object(sequencer, "_send_via_smtp", AsyncMock(return_value="<mock@example.com>")) as send:
            first, second = await asyncio.gather(*[
                sequencer.send_single_email(inbox, "person@example.com", "Intro", "Body", lead_id=1)
                for _ in range(2)
            ])
            assert sum(result["success"] for result in (first, second)) == 1
            assert send.await_count == 1
            assert await sequencer.get_next_available_inbox(1) is None
        conn = await database.get_db_connection()
        try:
            lead = await (await conn.execute("SELECT status, dispatch_claim FROM leads")).fetchone()
            assert tuple(lead) == ("sent", None)
        finally:
            await conn.close()
    asyncio.run(check())


def test_public_fetch_rejects_private_destination():
    result = asyncio.run(scraper.scrape_company_website("http://127.0.0.1"))
    assert result["crawler"] == "failed"
    assert "public unicast" in result["error"]


def test_provider_routes_require_tls():
    with pytest.raises(ValueError, match="HTTPS"):
        sequencer.provider_base_url("http://relay.example.com")
    assert sequencer.provider_base_url("relay.example.com") == "https://relay.example.com"
    assert ssl.create_default_context().verify_mode == ssl.CERT_REQUIRED


def test_ambiguous_send_requires_review(client):
    async def check():
        await insert_lead(status="queued")
        inbox = await sequencer.get_next_available_inbox(1)
        with patch.object(sequencer, "_send_via_smtp", AsyncMock(side_effect=TimeoutError)):
            result = await sequencer.send_single_email(inbox, "person@example.com", "Intro", "Body", lead_id=1)
        assert not result["success"]
        conn = await database.get_db_connection()
        try:
            row = await (await conn.execute("SELECT status, dispatch_claim FROM leads")).fetchone()
            assert tuple(row) == ("send_review", None)
        finally:
            await conn.close()
    asyncio.run(check())


def test_restart_recovers_legacy_tag_and_stranded_send(client):
    async def check():
        await insert_lead(status="meeting_booked")
        conn = await database.get_db_connection()
        try:
            await conn.execute("UPDATE campaigns SET status = 'sending'")
            await conn.execute("INSERT INTO leads (campaign_id, status, dispatch_claim) VALUES (1, 'queued', 0)")
            await conn.commit()
        finally:
            await conn.close()
        await database.init_db()
        conn = await database.get_db_connection()
        try:
            rows = await (await conn.execute("SELECT mailbox_tag, status, dispatch_claim FROM leads ORDER BY id")).fetchall()
            assert [tuple(row) for row in rows] == [("meeting_booked", "sent", None), (None, "send_review", None)]
            assert (await (await conn.execute("SELECT status FROM campaigns")).fetchone())[0] == "error"
        finally:
            await conn.close()
    asyncio.run(check())


def test_cloudflare_setup_preserves_existing_mail_records():
    import httpx
    from app.engine import cloudflare

    zone_name = "example.com"
    calls = []

    def respond(request):
        calls.append(request.method)
        if request.url.path.endswith("/dns_records"):
            return httpx.Response(200, json={"success": True, "result": [{"content": "mx.example.com"}]})
        return httpx.Response(200, json={"result": {"name": zone_name}})

    async def check():
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(cloudflare.httpx, "AsyncClient", return_value=client):
            result = await cloudflare.auto_setup_cloudflare_domain("test", "a" * 32, zone_name, "owner@example.com")
        assert not result["records_created"]
        assert "Existing MX" in result["errors"][0]
        assert calls == ["GET", "GET"]
    asyncio.run(check())


def _make_campaign(client):
    response = client.post("/api/campaigns", auth=AUTH, json={
        "name": "Import test", "icp_prompt": "Clinics", "value_proposition": "Faster intake",
    })
    assert response.status_code == 200
    return response.json()["id"]


def test_campaign_patch_sets_sender_settings(client):
    with patch("app.api.campaigns.run_discovery_task", new=AsyncMock()):
        campaign_id = _make_campaign(client)
    response = client.patch(f"/api/campaigns/{campaign_id}", auth=AUTH, json={
        "reply_to_email": "replies@example.com",
        "booking_url": "https://cal.example.com/intro",
        "sender_name": "Raymond",
        "knowledge_base": "We integrate with Epic.",
        "followup_1_days": 5,
    })
    assert response.status_code == 200
    camp = client.get(f"/api/campaigns/{campaign_id}", auth=AUTH).json()
    assert camp["reply_to_email"] == "replies@example.com"
    assert camp["sender_name"] == "Raymond"
    assert camp["followup_1_days"] == 5
    assert client.patch(f"/api/campaigns/{campaign_id}", auth=AUTH, json={"inbox_id": 999}).status_code == 400
    assert client.patch(f"/api/campaigns/{campaign_id}", auth=AUTH, json={"reply_to_email": "nope"}).status_code == 422
    assert client.patch(f"/api/campaigns/{campaign_id}", auth=AUTH, json={"status": "sending"}).status_code == 400


def test_csv_import_dedupes_suppresses_and_never_queues(client):
    with patch("app.api.campaigns.run_discovery_task", new=AsyncMock()):
        campaign_id = _make_campaign(client)
    assert client.post("/api/leads/suppression", auth=AUTH, json={"value": "blocked.com", "entry_type": "domain"}).status_code in (200, 201)
    body = (
        "Email,First_Name,Company_Name,verification_status\n"
        "ann@clinic.com,Ann,Clinic,verified\n"
        "ANN@clinic.com,Ann,Clinic,verified\n"
        "bob@blocked.com,Bob,Blocked,verified\n"
        "not-an-email,X,Y,\n"
        "cy@other.com,Cy,Other,trust-me\n"
    )
    response = client.post(f"/api/campaigns/{campaign_id}/import", auth=AUTH, json={"csv": body})
    assert response.status_code == 200
    assert response.json() == {"added": 2, "skipped_duplicate": 1, "skipped_suppressed": 1, "skipped_invalid": 1}
    leads = {l["email"]: l for l in client.get(f"/api/campaigns/{campaign_id}", auth=AUTH).json()["leads"]}
    assert set(leads) == {"ann@clinic.com", "cy@other.com"}
    assert all(l["status"] == "enriched" for l in leads.values())
    assert leads["cy@other.com"]["verification_status"] == "unverified"
    assert leads["ann@clinic.com"]["contact_name"] == "Ann"


def test_followup_uses_campaign_template():
    from app.engine.scheduler import followup_body
    lead = {"followup_step": 1, "followup_1_body": "Hi $first_name, any thoughts on $company? $booking_link",
            "company_name": "Clinic", "booking_url": "https://cal.example.com/x"}
    text = followup_body(lead, "Ann")
    assert text.startswith("Hi Ann, any thoughts on Clinic?")
    default = followup_body({"followup_step": 2}, "Ann")
    assert "Ann" in default and "$" not in default
