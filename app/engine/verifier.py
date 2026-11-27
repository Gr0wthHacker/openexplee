import asyncio
import re
import dns.resolver
from email_validator import validate_email, EmailNotValidError
from typing import Dict, Any, List
import aiosmtplib
import httpx
from app.config import settings
from app.engine.public_network import resolve_public_address

DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "tempmail.com", "10minutemail.com",
    "sharklasers.com", "getairmail.com", "dispostable.com", "yopmail.com"
}


def generate_email_permutations(first_name: str, last_name: str, domain: str) -> List[str]:
    """Generates standard B2B email permutations from name and domain."""
    f = re.sub(r"[^a-zA-Z]", "", first_name.lower().strip())
    l = re.sub(r"[^a-zA-Z]", "", last_name.lower().strip())
    d = domain.lower().replace("http://", "").replace("https://", "").replace("www.", "").strip("/ ")

    if not f and not l:
        return [f"contact@{d}", f"hello@{d}", f"info@{d}"]
    if not l:
        return [f"{f}@{d}", f"contact@{d}"]
    if not f:
        return [f"{l}@{d}", f"contact@{d}"]

    return [
        f"{f}.{l}@{d}",
        f"{f}@{d}",
        f"{f[0]}{l}@{d}",
        f"{f}{l}@{d}",
        f"{f[0]}.{l}@{d}",
        f"{l}@{d}",
    ]


async def get_mx_records(domain: str) -> List[str]:
    """Performs async DNS MX record lookup, sorted by preference."""
    loop = asyncio.get_running_loop()
    try:
        def _resolve():
            answers = dns.resolver.resolve(domain, "MX")
            records = sorted(answers, key=lambda r: r.preference)
            return [str(r.exchange).rstrip(".") for r in records if str(r.exchange) != "."]

        return await loop.run_in_executor(None, _resolve)
    except Exception:
        return []


async def verify_email_smtp(email: str, timeout: float = 2.0) -> Dict[str, Any]:
    """
    Validates email address through 4 tiers:
    1. Syntax (RFC standard)
    2. Disposable domain check
    3. DNS MX record check
    4. Non-intrusive SMTP Handshake (EHLO -> MAIL FROM -> RCPT TO -> QUIT)
    """
    result = {
        "email": email,
        "status": "unverified",  # 'verified', 'deliverable', 'catch_all', 'invalid', 'risky'
        "score": 0,
        "mx_host": None,
        "is_disposable": False,
        "details": ""
    }

    # Tier 1: Syntax Validation
    try:
        valid = validate_email(email, check_deliverability=False)
        email = valid.normalized
        domain = email.split("@")[1].lower()
    except EmailNotValidError as e:
        result["status"] = "invalid"
        result["details"] = f"Invalid email format: {str(e)}"
        return result

    # Tier 2: Disposable Check
    if domain in DISPOSABLE_DOMAINS:
        result["status"] = "invalid"
        result["is_disposable"] = True
        result["details"] = "Disposable burner email provider"
        return result

    # Tier 3: DNS MX Check
    mx_hosts = await get_mx_records(domain)
    if not mx_hosts:
        result["status"] = "invalid"
        result["details"] = f"Domain {domain} has no valid MX mail servers."
        return result

    primary_mx = mx_hosts[0]
    result["mx_host"] = primary_mx

    # Tier 4: Direct SMTP Handshake check
    smtp_client = None
    try:
        address = await resolve_public_address(primary_mx, 25)
        smtp_client = aiosmtplib.SMTP(
            hostname=address,
            port=25,
            timeout=timeout,
            use_tls=False,
            start_tls=False
        )
        await smtp_client.connect()
        await smtp_client.ehlo("verify.openexplee.local")
        
        await smtp_client.mail("check@verify.openexplee.local")
        await smtp_client.rcpt(email)
        try:
            await smtp_client.rcpt(f"check_catchall_987654@{domain}")
        except aiosmtplib.SMTPRecipientRefused as exc:
            if exc.code in (550, 551, 553):
                result.update(status="verified", score=99, details="Target was accepted while the test address was refused.")
            else:
                result.update(status="risky", score=40, details="Target was accepted, but the catch-all check was inconclusive.")
        else:
            result.update(status="catch_all", score=70, details="Server accepted both the target and the test address.")

    except aiosmtplib.SMTPRecipientRefused as exc:
        if exc.code in (550, 551, 553):
            result.update(status="invalid", score=0, details=f"Target rejected by the server (SMTP {exc.code}).")
        else:
            result.update(status="risky", score=40, details=f"Target verification inconclusive (SMTP {exc.code}).")
    except Exception as e:
        # Port 25 blocked or timed out: the domain takes mail, but nobody asked about this mailbox.
        result["status"] = "risky"
        result["score"] = 40
        result["details"] = f"Domain has mail servers ({primary_mx}). Mailbox not checked ({type(e).__name__})."

    finally:
        if smtp_client is not None:
            smtp_client.close()

    return result


MILLIONVERIFIER_URL = "https://api.millionverifier.com/api/v3/"

# MillionVerifier results mapped onto this app's statuses. It charges only for ok, invalid
# and disposable; catch-all and unknown are free.
_MV_STATUS = {
    "ok": ("verified", 99, "MillionVerifier confirmed the mailbox exists."),
    "catch_all": ("catch_all", 70, "MillionVerifier says the domain accepts every address."),
    "invalid": ("invalid", 0, "MillionVerifier says the mailbox does not exist."),
    "disposable": ("invalid", 0, "MillionVerifier says this is a disposable address."),
    "unknown": ("risky", 40, "MillionVerifier could not tell whether the mailbox exists."),
}


async def _verify_via_millionverifier(email: str, base: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    result = {**base, "checked_by": "millionverifier"}
    try:
        async with httpx.AsyncClient(timeout=timeout + 10) as client:
            resp = await client.get(MILLIONVERIFIER_URL, params={
                "api": settings.millionverifier_api_key or "",
                "email": email,
                "timeout": int(max(2, min(60, timeout))),
            })
        data = resp.json() if resp.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as exc:
        result.update(status="risky", score=40, details=f"Verifier request failed ({type(exc).__name__}).")
        return result
    if not data or data.get("error"):
        result.update(status="risky", score=40, details=f"Verifier error: {str(data.get('error') or resp.status_code)[:80]}")
        return result
    key = str(data.get("result", "")).lower().replace("-", "_").replace(" ", "_")
    if key == "catchall":
        key = "catch_all"
    status, score, details = _MV_STATUS.get(key, ("risky", 40, f"Verifier returned an unrecognized result ({key or 'none'})."))
    result.update(status=status, score=score, details=details)
    return result


async def verify_email(email: str, timeout: float = 5.0) -> Dict[str, Any]:
    """Checks an address with the configured verifier: a paid API, or the SMTP handshake."""
    if settings.email_verifier == "millionverifier" and settings.millionverifier_api_key:
        base = {"email": email, "status": "unverified", "score": 0, "mx_host": None,
                "is_disposable": False, "details": ""}
        try:
            email = validate_email(email, check_deliverability=False).normalized
        except EmailNotValidError as e:
            return {**base, "status": "invalid", "details": f"Invalid email format: {e}", "checked_by": "syntax"}
        domain = email.split("@")[1].lower()
        if domain in DISPOSABLE_DOMAINS:
            return {**base, "status": "invalid", "is_disposable": True, "details": "Disposable burner email provider", "checked_by": "syntax"}
        mx_hosts = await get_mx_records(domain)
        if not mx_hosts:
            return {**base, "status": "invalid", "details": f"Domain {domain} has no valid MX mail servers.", "checked_by": "dns"}
        return await _verify_via_millionverifier(email, {**base, "email": email, "mx_host": mx_hosts[0]}, timeout)
    res = await verify_email_smtp(email, timeout=timeout)
    return {**res, "checked_by": "smtp"}


async def enrich_person_email(first_name: str, last_name: str, domain: str) -> Dict[str, Any]:
    """
    Replicates Explee's enrich_email tool for $0: generates corporate email
    permutations and checks DNS MX + live SMTP handshake to find the best address.
    """
    candidates = generate_email_permutations(first_name, last_name, domain)[: settings.max_email_guesses]

    for candidate in candidates:
        res = await verify_email(candidate, timeout=5.0)
        # Only a mailbox the mail server confirmed (or a catch-all domain) counts as found.
        if res["status"] in ("verified", "catch_all"):
            return {**res, "candidates_tested": candidates}
        if res["mx_host"] is None:
            # Domain has no MX records at all; stop probing permutations
            return {**res, "candidates_tested": [candidate]}
        if res["status"] == "risky" and res.get("checked_by") == "smtp":
            # The mail server could not be reached. The other patterns will fail the same way.
            break

    # A name pattern nobody confirmed is a guess, and guesses bounce. Report it as not sendable.
    return {
        "email": candidates[0] if candidates else None,
        "status": "invalid",
        "score": 0,
        "mx_host": None,
        "is_disposable": False,
        "details": "Guessed from a name pattern. No mail server confirmed the mailbox.",
        "candidates_tested": candidates,
    }

