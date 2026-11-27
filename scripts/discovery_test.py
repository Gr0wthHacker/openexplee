"""Dry run of lead discovery. Sends no email and writes nothing to the app's database.

It searches for companies, reads their sites, pulls out names, guesses addresses and
asks each company's mail server whether a guess exists (SMTP RCPT, then QUIT). It also
checks whether this machine can reach mail servers on port 25 at all.

Usage:
    python scripts/discovery_test.py "query one" "query two" --sites 10 --out results.json
"""
import argparse
import asyncio
import json
import os
import socket
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

# Keep the app's real database untouched.
os.environ.setdefault("DATABASE_PATH", str(Path(tempfile.gettempdir()) / "openexplee-discovery-test.db"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import database  # noqa: E402
from app.config import settings  # noqa: E402
from app.engine import discovery, verifier  # noqa: E402
from app.engine.scraper import scrape_company_website  # noqa: E402


def port_25_open(host: str = "gmail-smtp-in.l.google.com", timeout: float = 8.0) -> dict:
    try:
        with socket.create_connection((host, 25), timeout=timeout) as s:
            banner = s.recv(200).decode(errors="replace").strip()
            s.sendall(b"QUIT\r\n")
        return {"open": True, "host": host, "banner": banner[:80]}
    except Exception as exc:
        return {"open": False, "host": host, "error": f"{type(exc).__name__}: {exc}"}


async def search(query: str, n: int) -> tuple[str, list]:
    if settings.serper_api_key:
        found = await discovery.search_serper(query, settings.serper_api_key, n)
        if found:
            return "serper", found
    found = await discovery.search_bing(query, n)
    if found:
        return "bing", found
    found = await discovery.search_duckduckgo(query, n)
    return ("duckduckgo" if found else "none"), found


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="+")
    ap.add_argument("--sites", type=int, default=10, help="most company sites to read")
    ap.add_argument("--out", default="discovery-results.json")
    args = ap.parse_args()

    started = time.time()
    await database.init_db()
    report = {"port_25": port_25_open(), "llm_key_set": any([settings.gemini_api_key, settings.openai_api_key,
                                                       settings.anthropic_api_key, settings.groq_api_key]),
              "searches": [], "sites": []}

    # 1. Search
    seen, candidates = set(), []
    for q in args.queries:
        engine, found = await search(q, args.sites * 2)
        report["searches"].append({"query": q, "engine": engine, "results": len(found)})
        for r in found:
            d = (r.get("domain") or "").lower()
            if d and d not in seen and not any(skip in d for skip in discovery.NON_COMPANY_HOSTS):
                seen.add(d)
                candidates.append(r)
        await asyncio.sleep(2)  # be gentle with free search pages

    # 2. Read sites, pull people, guess and check addresses
    for site in candidates[: args.sites]:
        t = time.time()
        intel = await scrape_company_website(site["url"])
        row = {"domain": site.get("domain"), "scraped": not intel.get("error"), "error": intel.get("error")}
        if intel.get("error"):
            report["sites"].append(row)
            continue
        domain = intel["domain"]
        own = [e for e in intel.get("found_emails", []) if e.endswith("@" + domain) or e.endswith("." + domain)]
        people = await discovery._extract_people_with_llm(domain, intel.get("content_summary", ""))
        row.update(people_found=len(people), site_emails=len(own))
        if people:
            p = people[0]
            enriched = await verifier.enrich_person_email(p["first_name"], p["last_name"], domain)
            row.update(person_title=p.get("job_title"), guess_status=enriched.get("status"),
                       guesses_tried=len(enriched.get("candidates_tested", [])), detail=enriched.get("details"))
        elif own:
            check = await verifier.verify_email_smtp(own[0], timeout=5.0)
            row.update(guess_status="site_email_" + check["status"], detail=check.get("details"))
        else:
            row.update(guess_status="no_contact")
        row["seconds"] = round(time.time() - t, 1)
        report["sites"].append(row)

    statuses = Counter(s.get("guess_status", "not_scraped") for s in report["sites"])
    usable = sum(n for k, n in statuses.items() if k in ("verified", "site_email_verified"))
    report["summary"] = {
        "port_25_open": report["port_25"]["open"],
        "llm_key_set": report["llm_key_set"],
        "search_engines_used": Counter(s["engine"] for s in report["searches"]),
        "companies_found": len(candidates),
        "sites_read": sum(1 for s in report["sites"] if s["scraped"]),
        "sites_tried": len(report["sites"]),
        "sites_with_a_named_person": sum(1 for s in report["sites"] if s.get("people_found")),
        "statuses": statuses,
        "verified_leads": usable,
        "catch_all": statuses.get("catch_all", 0) + statuses.get("site_email_catch_all", 0),
        "minutes": round((time.time() - started) / 60, 1),
    }
    # Domains stay in the local file only; the printed summary has counts.
    Path(args.out).write_text(json.dumps(report, indent=2, default=dict))
    print(json.dumps(report["summary"], indent=2, default=dict))
    print(f"Full results: {Path(args.out).resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
