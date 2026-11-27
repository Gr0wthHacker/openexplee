import asyncio
import re
from typing import Dict, Any, Set
from urllib.parse import urljoin, urlparse
import httpx
from app.engine.public_network import resolve_public_address
from bs4 import BeautifulSoup

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Ordered by how likely the page is to name the people in charge.
SUBPAGE_KEYWORDS = (
    "team",
    "leadership",
    "founder",
    "people",
    "about",
    "staff",
    "provider",
    "contact",
    "product",
    "feature",
    "pricing",
    "platform",
    "careers",
)

IGNORED_EMAIL_PATTERNS = (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", "sentry", "example", "wixpress")


def _clean_emails(raw_text: str) -> Set[str]:
    """Finds and filters valid email addresses from text or markup."""
    valid = set()
    for match in EMAIL_REGEX.findall(raw_text or ""):
        m_lower = match.lower()
        if not any(bad in m_lower for bad in IGNORED_EMAIL_PATTERNS):
            valid.add(m_lower)
    return valid


def _page_text(soup: BeautifulSoup, limit: int) -> str:
    """Visible text with element breaks kept, so "Jane Doe | Founder" stays readable."""
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    return soup.get_text(separator=" | ", strip=True)[:limit]


def _subpage_rank(url: str, text: str) -> int:
    hay = f"{text} {url}".lower()
    for i, kw in enumerate(SUBPAGE_KEYWORDS):
        if kw in hay:
            return i
    return len(SUBPAGE_KEYWORDS)


async def fetch_public_html(url: str) -> tuple[str, str]:
    async with asyncio.timeout(30):
        for _ in range(5):
            target = httpx.URL(url)
            if (target.scheme not in {"http", "https"} or not target.host
                    or target.userinfo or target.port not in {None, 80, 443}):
                raise ValueError("Use a public HTTP or HTTPS website on a standard port.")
            port = target.port or (443 if target.scheme == "https" else 80)
            address = await resolve_public_address(target.host, port)
            pinned = target.copy_with(host=address)
            # Fresh pool per hop keeps TLS identity tied to the original hostname.
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                async with client.stream(
                    "GET", pinned,
                    # Many sites and CDNs compress no matter what we ask for, so accept it and decode.
                    headers={**DEFAULT_HEADERS, "Host": target.netloc.decode(), "Accept-Encoding": "gzip, deflate"},
                    extensions={"sni_hostname": target.raw_host.decode()},
                ) as response:
                    if response.is_redirect and "location" in response.headers:
                        url = urljoin(url, response.headers["location"])
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").split(";")[0].lower()
                    if content_type not in {"text/html", "application/xhtml+xml"}:
                        raise ValueError("The website did not return HTML.")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 2_000_000:
                            raise ValueError("HTML response exceeds 2 MB.")
                        body.extend(chunk)
                    return url, body.decode(response.encoding or "utf-8", errors="replace")
    raise ValueError("Too many website redirects.")


async def _scrape_html(url: str, max_depth: int = 1) -> Dict[str, Any]:
    """
    Extract company information from public HTML.
    """
    url, html = await fetch_public_html(url)
    parsed = urlparse(url)
    domain = (parsed.hostname or "").removeprefix("www.")

    intel: Dict[str, Any] = {
        "domain": domain,
        "url": url,
        "title": "",
        "description": "",
        "headlines": [],
        "content_summary": "",
        "found_emails": set(),
        "team_leads": [],
        "subpages": [],
        "crawler": "public_html",
    }

    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        intel["title"] = soup.title.string.strip()

    meta_desc = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta_desc and meta_desc.get("content"):
        intel["description"] = meta_desc["content"].strip()

    for h in soup.find_all(["h1", "h2"])[:6]:
        txt = h.get_text(strip=True)
        if txt and len(txt) > 5:
            intel["headlines"].append(txt)

    intel["found_emails"].update(_clean_emails(html))

    about_urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True).lower()
        full_url = urljoin(url, href).split("#")[0]
        if urlparse(full_url).netloc == parsed.netloc:
            if any(kw in text or kw in href.lower() for kw in SUBPAGE_KEYWORDS):
                if full_url not in [u for u, _ in about_urls] and full_url.rstrip("/") != url.rstrip("/"):
                    about_urls.append((full_url, text))
    about_urls.sort(key=lambda pair: _subpage_rank(pair[0], pair[1]))

    intel["content_summary"] = _page_text(soup, 2500)

    if max_depth > 0:
        for sub_url, _ in about_urls[:4]:
            try:
                sub_final, sub_html = await fetch_public_html(sub_url)
                if urlparse(sub_final).netloc == parsed.netloc:
                    sub_soup = BeautifulSoup(sub_html, "html.parser")
                    intel["found_emails"].update(_clean_emails(sub_html))
                    sub_text = _page_text(sub_soup, 3000)
                    intel["content_summary"] += f"\n[{sub_url}]: {sub_text}"
                    intel["subpages"].append(sub_url)
            except Exception:
                pass

    intel["found_emails"] = sorted(list(intel["found_emails"]))
    return intel


async def scrape_company_website(url: str, max_depth: int = 1) -> Dict[str, Any]:
    """Collect company information without executing scripts or loading subresources."""
    if "://" not in url:
        url = "https://" + url
    try:
        return await _scrape_html(url, max_depth=max_depth)
    except (OSError, TimeoutError, httpx.TransportError) as first:
        # Some sites answer only on www. (or only without it). Try the other once.
        host = urlparse(url).hostname or ""
        alt_host = host[4:] if host.startswith("www.") else "www." + host
        try:
            return await _scrape_html(url.replace(host, alt_host, 1), max_depth=max_depth)
        except (ValueError, OSError, TimeoutError, httpx.HTTPError, httpx.InvalidURL):
            exc = first
    except (ValueError, httpx.HTTPError, httpx.InvalidURL) as err:
        exc = err
    return {
        "domain": "", "url": url, "title": "", "description": "",
        "headlines": [], "content_summary": "", "found_emails": [],
        "team_leads": [], "subpages": [], "error": f"{type(exc).__name__}: {exc}"[:200], "crawler": "failed",
    }
