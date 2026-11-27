import json
import re
from typing import Any, Dict, Optional
import httpx
from app.config import settings

def _extract_json_blob(text: str) -> Any:
    """Extracts JSON object or array from raw LLM text with multi-pattern tolerance."""
    cleaned = text.strip()
    if "```" in cleaned:
        m_fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
        if m_fence:
            cleaned = m_fence.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    m_obj = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", cleaned)
    if m_obj:
        try:
            return json.loads(m_obj.group(1))
        except json.JSONDecodeError:
            pass

    # Tolerant extraction for individual JSON objects: [{"a": 1}, {"b": 2}] or multiple lines
    objs = re.findall(r"\{[^{}]+\}", cleaned)
    if objs:
        extracted = []
        for o in objs:
            try:
                extracted.append(json.loads(o))
            except Exception:
                pass
        if extracted:
            return extracted

    # Tolerant extraction for strings / arrays of strings
    arrays = re.findall(r'\[\s*"[^"]+"\s*\]', cleaned)
    if arrays:
        items = []
        for a in arrays:
            try:
                items.extend(json.loads(a))
            except Exception:
                pass
        if items:
            return items

    # Fallback: extract list of quoted lines (e.g. 1. "query one" 2. "query two")
    quoted_lines = re.findall(r'"([^"\n]{3,120})"', cleaned)
    if quoted_lines and len(quoted_lines) >= 2:
        return quoted_lines

    raise ValueError("No valid JSON found in LLM response")


async def _call_openai_compatible(base_url: str, model: str, prompt: str, api_key: Optional[str] = None, json_mode: bool = False, timeout: float = 60.0) -> Optional[str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{base_url.rstrip('/')}/v1/chat/completions", headers=headers, json=payload)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        print(f"[LLM:openai_compat] HTTP {resp.status_code}: {resp.text[:200]}")
    return None


async def _call_gemini(prompt: str, api_key: str) -> Optional[str]:
    model = settings.default_llm_model or "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.post(url, json=payload, headers={"x-goog-api-key": api_key})
        if resp.status_code == 200:
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    return None


async def _call_anthropic(prompt: str, api_key: str) -> Optional[str]:
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-3-5-haiku-latest",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        if resp.status_code == 200:
            return resp.json()["content"][0]["text"]
    return None


async def call_llm_json(prompt: str, provider: Optional[str] = None, api_key: Optional[str] = None) -> Optional[Any]:
    """Send the prompt to the explicitly selected provider."""
    prov = provider or settings.default_llm_provider
    raw_text: Optional[str] = None
    try:
        if prov == "ollama":
            # Local models are slower, so give them longer.
            raw_text = await _call_openai_compatible(
                settings.ollama_base_url, settings.ollama_model, prompt, timeout=180.0
            )
        elif prov == "gemini" and (api_key or settings.gemini_api_key):
            raw_text = await _call_gemini(prompt, api_key or settings.gemini_api_key or "")
        elif prov == "openai" and (api_key or settings.openai_api_key):
            raw_text = await _call_openai_compatible(
                "https://api.openai.com", "gpt-4o-mini", prompt, api_key or settings.openai_api_key, json_mode=True
            )
        elif prov == "groq" and (api_key or settings.groq_api_key):
            raw_text = await _call_openai_compatible(
                "https://api.groq.com/openai", "llama-3.3-70b-versatile", prompt, api_key or settings.groq_api_key, json_mode=True
            )
        elif prov == "anthropic" and (api_key or settings.anthropic_api_key):
            raw_text = await _call_anthropic(prompt, api_key or settings.anthropic_api_key or "")

        if raw_text:
            return _extract_json_blob(raw_text)
    except Exception as exc:
        print(f"[LLM:{prov}] {type(exc).__name__}")

    return None
