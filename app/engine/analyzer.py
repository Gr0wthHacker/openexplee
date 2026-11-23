from typing import Dict, Any
from pydantic import BaseModel, Field, ValidationError
from app.engine.scraper import scrape_company_website
from app.engine.llm import call_llm_json


class SuggestedICP(BaseModel):
    campaign_name: str = Field(min_length=1, max_length=160)
    icp_query: str = Field(min_length=1, max_length=2000)


class ProductAnalysis(BaseModel):
    product_name: str = Field(min_length=1, max_length=200)
    value_proposition: str = Field(min_length=1, max_length=2000)
    key_features: list[str] = Field(default_factory=list, max_length=10)
    target_icps: list[SuggestedICP] = Field(max_length=8)


async def analyze_my_product(company_url: str) -> Dict[str, Any]:
    """
    Deep-crawls the user's company website (e.g. https://example.com),
    extracts all product features & positioning, and generates targeted ICP campaigns.
    Uses the configured language model, if available.
    """
    intel = await scrape_company_website(company_url)
    if intel.get("error"):
        raise ValueError("Could not read the submitted public website.")
    content_text = (
        f"Title: {intel.get('title')}\n"
        f"Description: {intel.get('description')}\n"
        f"Headlines: {', '.join(intel.get('headlines', []))}\n"
        f"Subpages Crawled: {', '.join(intel.get('subpages', []))}\n"
        f"Summary:\n{intel.get('content_summary', '')[:4500]}"
    )

    prompt = f"""
You are a world-class Chief Revenue Officer & B2B Go-To-Market strategist.

Analyze our company website:
{content_text}

Extract and generate the following in valid JSON format:
1. "product_name": Clean name of the primary product.
2. "value_proposition": A crisp 1-sentence value proposition explaining what it solves and for whom.
3. "key_features": List of 3-5 core capabilities extracted from the site.
4. "target_icps": A list of 4 high-converting target market segments (Ideal Customer Profiles), each with:
   - "campaign_name": Short name (e.g. "Enterprise AI Builders", "Agentic Startups", "AI Ops Leaders")
   - "icp_query": Natural language search query to find these target companies on the web
   - "pain_point": Specific pain point they experience that our product solves
   - "cold_pitch": A 2-sentence conversational cold email hook

Return strictly valid JSON with keys: "product_name", "value_proposition", "key_features", "target_icps".
"""

    parsed = await call_llm_json(prompt)
    try:
        result = ProductAnalysis.model_validate(parsed).model_dump()
    except ValidationError:
        result = {
            "product_name": intel.get("title") or intel["domain"],
            "value_proposition": intel.get("description") or "",
            "key_features": intel.get("headlines", [])[:4],
            "target_icps": [],
        }
    result["crawled_subpages"] = intel.get("subpages", [])
    return result
