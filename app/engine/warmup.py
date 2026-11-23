import dns.asyncresolver
from typing import Dict, Any


async def check_domain_dns_health(domain: str) -> Dict[str, Any]:
    """
    Audits domain DNS records (SPF, DMARC, MX) to ensure cold outreach deliverability.
    """
    clean_domain = domain.lower().replace("http://", "").replace("https://", "").replace("www.", "").strip("/ ")
    report = {
        "domain": clean_domain,
        "has_mx": False,
        "has_spf": False,
        "has_dmarc": False,
        "spf_record": None,
        "dmarc_record": None,
        "score": 0,
        "recommendations": []
    }

    # 1. Check MX
    try:
        mx_answers = await dns.asyncresolver.resolve(clean_domain, "MX", lifetime=4)
        if any(str(record.exchange) != "." for record in mx_answers):
            report["has_mx"] = True
            report["score"] += 25
    except Exception:
        report["recommendations"].append("Missing MX records: Domain cannot receive email or bounces.")

    # 2. Check SPF (TXT records on root domain)
    try:
        txt_answers = await dns.asyncresolver.resolve(clean_domain, "TXT", lifetime=4)
        for rdata in txt_answers:
            txt_string = "".join([part.decode("utf-8") if isinstance(part, bytes) else str(part) for part in rdata.strings])
            if "v=spf1" in txt_string:
                report["has_spf"] = True
                report["spf_record"] = txt_string
                report["score"] += 35
                break
        if not report["has_spf"]:
            report["recommendations"].append("Missing SPF record: Add 'v=spf1 include:_spf.google.com ~all' (or your host SPF).")
    except Exception:
        report["recommendations"].append("Failed to query SPF TXT records.")

    # 3. Check DMARC (TXT record on _dmarc.domain)
    try:
        dmarc_answers = await dns.asyncresolver.resolve(f"_dmarc.{clean_domain}", "TXT", lifetime=4)
        for rdata in dmarc_answers:
            txt_string = "".join([part.decode("utf-8") if isinstance(part, bytes) else str(part) for part in rdata.strings])
            if "v=DMARC1" in txt_string:
                report["has_dmarc"] = True
                report["dmarc_record"] = txt_string
                report["score"] += 40
                break
        if not report["has_dmarc"]:
            report["recommendations"].append("Missing DMARC: Add TXT at _dmarc." + clean_domain + " with 'v=DMARC1; p=none; sp=none;'")
    except Exception:
        report["recommendations"].append("Missing DMARC: Add TXT record at _dmarc." + clean_domain)

    return report
