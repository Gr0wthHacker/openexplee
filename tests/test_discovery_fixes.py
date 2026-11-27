import asyncio
import functools
import gzip
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.config import settings
from app.engine import discovery, scraper, verifier


def run_async(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapper


def test_title_regex_reads_separated_names():
    text = "Meet the team | Jane Doe | Founder & CEO | Dr. Maria Lopez, MD | Medical Director | Learn More | Owner"
    people = [(m.group(1), m.group(2)) for m in discovery.PERSON_TITLE_REGEX.finditer(text)]
    assert ("Jane Doe", "Founder") in people
    assert ("Maria Lopez", "Medical Director") in people


@run_async
async def test_regex_fallback_skips_button_text():
    with patch("app.engine.llm.call_llm_json", AsyncMock(return_value=None)):
        people = await discovery._extract_people_with_llm("x.com", "Learn More | Owner | Sam Reyes | Owner")
    assert [p["first_name"] for p in people] == ["Sam"]


@run_async
async def test_llm_wrapped_list_is_accepted():
    wrapped = {"people": [{"first_name": "Ana", "last_name": "Kim", "job_title": "CEO"}]}
    with patch("app.engine.llm.call_llm_json", AsyncMock(return_value=wrapped)):
        people = await discovery._extract_people_with_llm("x.com", "some text")
    assert people[0]["last_name"] == "Kim"


def test_subpages_ranked_team_first():
    assert scraper._subpage_rank("https://x.com/pricing", "pricing") > scraper._subpage_rank("https://x.com/our-team", "team")


def _mv_response(payload, status=200):
    return httpx.Response(status, json=payload, request=httpx.Request("GET", verifier.MILLIONVERIFIER_URL))


@pytest.mark.parametrize("result,expected", [
    ("ok", "verified"), ("catch_all", "catch_all"), ("invalid", "invalid"),
    ("disposable", "invalid"), ("unknown", "risky"), ("something_new", "risky"),
])
@run_async
async def test_millionverifier_mapping(monkeypatch, result, expected):
    monkeypatch.setattr(settings, "email_verifier", "millionverifier")
    monkeypatch.setattr(settings, "millionverifier_api_key", "test-key")
    with patch.object(verifier, "get_mx_records", AsyncMock(return_value=["mx.example.com"])), \
         patch("httpx.AsyncClient.get", AsyncMock(return_value=_mv_response({"result": result, "error": ""}))):
        res = await verifier.verify_email("jane@example-co.com")
    assert res["status"] == expected
    assert res["checked_by"] == "millionverifier"


@run_async
async def test_millionverifier_error_is_risky(monkeypatch):
    monkeypatch.setattr(settings, "email_verifier", "millionverifier")
    monkeypatch.setattr(settings, "millionverifier_api_key", "bad")
    with patch.object(verifier, "get_mx_records", AsyncMock(return_value=["mx.example.com"])), \
         patch("httpx.AsyncClient.get", AsyncMock(return_value=_mv_response({"error": "invalid_api_key"}))):
        res = await verifier.verify_email("jane@example-co.com")
    assert res["status"] == "risky"


@run_async
async def test_enrich_keeps_guessing_on_api_unknown_and_caps_guesses(monkeypatch):
    monkeypatch.setattr(settings, "max_email_guesses", 3)
    calls = []

    async def fake_verify(email, timeout=5.0):
        calls.append(email)
        status = "verified" if len(calls) == 3 else "risky"
        return {"email": email, "status": status, "mx_host": "mx", "checked_by": "millionverifier"}

    with patch.object(verifier, "verify_email", fake_verify):
        res = await verifier.enrich_person_email("Jane", "Doe", "example-co.com")
    assert len(calls) == 3 and res["status"] == "verified"


@run_async
async def test_enrich_stops_when_smtp_port_is_blocked():
    calls = []

    async def fake_verify(email, timeout=5.0):
        calls.append(email)
        return {"email": email, "status": "risky", "mx_host": "mx", "checked_by": "smtp"}

    with patch.object(verifier, "verify_email", fake_verify):
        res = await verifier.enrich_person_email("Jane", "Doe", "example-co.com")
    assert len(calls) == 1 and res["status"] == "invalid"


@run_async
async def test_scraper_decodes_gzip(monkeypatch):
    html = b"<html><title>Acme</title><body><p>Jane Doe | Founder</p></body></html>"

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html", "content-encoding": "gzip"},
                              content=gzip.compress(html))

    real_client = httpx.AsyncClient
    monkeypatch.setattr(scraper.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(scraper, "resolve_public_address", AsyncMock(return_value="93.184.216.34"))
    url, body = await scraper.fetch_public_html("https://acme.example.com/")
    assert "Jane Doe" in body
