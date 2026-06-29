#!/usr/bin/env python3
"""
gemini_client.py — Gemini AI Client (ทำงานใน GitHub Actions โดยตรง)
ไม่ต้องผ่าน Netlify proxy อีกต่อไป — เรียก Gemini API ตรงๆ จาก Python

Env: GEMINI_API_KEY  (ตั้งใน GitHub Secrets)

Usage:
    from gemini_client import gemini_call, gemini_multimodal

    # Text only
    result = gemini_call("วิเคราะห์หุ้น AAPL ให้หน่อย")

    # Multimodal (image + text)
    result = gemini_multimodal(image_base64="...", image_mime="image/jpeg", text="วิเคราะห์โฆษณานี้")
"""

import json
import os
import time
import urllib.request
import urllib.error
from typing import Optional

# ─── Config ───────────────────────────────────────────────────────────────────
GEMINI_MODEL          = "gemini-2.5-flash"
GEMINI_FALLBACK_MODEL = "gemini-2.0-flash"
GEMINI_URL_BASE       = "https://generativelanguage.googleapis.com/v1beta/models/"
MAX_RETRY             = 2
RETRY_BASE_SEC        = 1.5


# ═══════════════════════════════════════════════════════════════════════════════
#  CORE CALLER
# ═══════════════════════════════════════════════════════════════════════════════

def _gemini_call_raw(contents: list, model: str, api_key: str,
                     max_tokens: int = 8192, temperature: float = 0.4) -> str:
    """Single Gemini API call — ไม่มี retry"""
    url     = f"{GEMINI_URL_BASE}{model}:generateContent?key={api_key}"
    payload = json.dumps({
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature":     temperature,
            "thinkingConfig":  {"thinkingBudget": 0},
        },
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        err  = Exception(f"Gemini HTTP {e.code}: {body[:300]}")
        err.status = e.code                                      # type: ignore[attr-defined]
        raise err

    text = (
        (data.get("candidates") or [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "")
    )
    return text


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status", None)
    msg    = str(exc).lower()
    return (
        status in (429, 500, 503) or
        "high demand"  in msg or
        "overloaded"   in msg or
        "try again"    in msg
    )


def _call_with_retry(contents: list, api_key: str,
                     max_tokens: int = 8192, temperature: float = 0.4) -> str:
    """Retry primary model → fallback model"""
    last_err: Optional[Exception] = None

    # ── Primary model ─────────────────────────────────────────────────────────
    for attempt in range(1, MAX_RETRY + 1):
        try:
            return _gemini_call_raw(contents, GEMINI_MODEL, api_key, max_tokens, temperature)
        except Exception as e:
            last_err = e
            if not _is_retryable(e) or attempt == MAX_RETRY:
                break
            time.sleep(RETRY_BASE_SEC * (2 ** (attempt - 1)))

    # ── Fallback model ────────────────────────────────────────────────────────
    if last_err and _is_retryable(last_err):
        time.sleep(1.0)
        for fb in range(1, MAX_RETRY + 1):
            try:
                return _gemini_call_raw(contents, GEMINI_FALLBACK_MODEL, api_key, max_tokens, temperature)
            except Exception as e:
                last_err = e
                if not _is_retryable(e) or fb == MAX_RETRY:
                    break
                time.sleep(RETRY_BASE_SEC * (2 ** (fb - 1)))

    raise last_err or Exception("Gemini: unknown error")


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════════

def gemini_call(prompt: str, max_tokens: int = 8192,
                temperature: float = 0.4, api_key: str = "") -> str:
    """
    Text-only Gemini call
    api_key ถ้าไม่ส่งมาจะดึงจาก env GEMINI_API_KEY อัตโนมัติ
    """
    key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise ValueError("GEMINI_API_KEY ไม่ได้ตั้งค่า — ใส่ใน GitHub Secrets")

    contents = [{"parts": [{"text": prompt}]}]
    return _call_with_retry(contents, key, max_tokens, temperature)


def gemini_multimodal(image_base64: str, text: str,
                      image_mime: str = "image/jpeg",
                      max_tokens: int = 8192, temperature: float = 0.4,
                      api_key: str = "") -> str:
    """
    Multimodal Gemini call (image + text)
    ใช้สำหรับ Ad Analyzer ใน Ad Prompt GodMode
    """
    key = api_key or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise ValueError("GEMINI_API_KEY ไม่ได้ตั้งค่า")
    if not image_base64:
        raise ValueError("image_base64 ต้องไม่ว่าง")
    if not text:
        raise ValueError("text prompt ต้องไม่ว่าง")

    contents = [{"parts": [
        {"text": text},
        {"inline_data": {"mime_type": image_mime, "data": image_base64}},
    ]}]
    return _call_with_retry(contents, key, max_tokens, temperature)


def gemini_json(prompt: str, max_tokens: int = 1024,
                temperature: float = 0.1, api_key: str = "") -> dict:
    """
    เรียก Gemini แล้ว parse JSON กลับมาเลย
    ใช้สำหรับ template selection, score analysis ฯลฯ
    """
    raw = gemini_call(prompt, max_tokens=max_tokens,
                      temperature=temperature, api_key=api_key)
    # Strip markdown fences ถ้ามี
    text = raw.strip()
    if "```" in text:
        parts = text.split("```")
        text  = parts[1] if len(parts) > 1 else parts[0]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI TEST
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    prompt = " ".join(sys.argv[1:]) or "สวัสดี ทดสอบ Gemini API"
    print(f"Prompt: {prompt}")
    print(f"Model: {GEMINI_MODEL}")
    try:
        result = gemini_call(prompt)
        print(f"\nResponse:\n{result}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
