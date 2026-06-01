"""Robust JSON parsing for LLM responses.

LLM responses often wrap JSON in ``` fences, prose, or trailing text.
This module centralises the strip-and-parse logic that used to be
duplicated across triage/engine.py, orchestrator/loop.py, and other
call sites.
"""
from __future__ import annotations

import json
import re


def strip_fence(s: str) -> str:
    """Remove leading/trailing markdown code fences if present."""
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```\w*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def safe_parse(raw: str) -> dict | None:
    """Try to extract a JSON object from arbitrary LLM output.
    Returns None if nothing parseable is found.
    """
    try:
        return json.loads(strip_fence(raw))
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None
