#!/usr/bin/env python3
"""
Verify OpenAI API is reachable. Bypasses cache.
Run from project root. Check platform.openai.com after - you should see the request.
"""
import os
from pathlib import Path

# Load .env
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from crtv.reasoning.llm_providers import OpenAICompatibleProvider

def main():
    provider = OpenAICompatibleProvider()
    if not provider.api_key:
        print("FAIL: CRTV_OPENAI_API_KEY not set in .env")
        return 1
    print(f"Calling OpenAI (model={provider.model})...")
    raw = provider.generate("Reply with exactly: OK")
    if raw and "ok" in raw.lower():
        print("OK: API responded:", raw[:100])
        return 0
    print("FAIL: Empty or unexpected response:", repr(raw))
    return 1

if __name__ == "__main__":
    exit(main())
