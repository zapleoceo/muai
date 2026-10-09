"""Ссылки задачи (refs): только указатели, содержимое по ним не читается."""
from __future__ import annotations

from typing import Any

REF_KINDS = ("jira", "url", "event", "chunk")
MAX_REFS = 20
MAX_REF_LEN = 500
MAX_EXCERPT_LEN = 300


def validate_refs(refs: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not isinstance(refs, list) or len(refs) > MAX_REFS:
        raise ValueError(f"refs must be a list of at most {MAX_REFS} items")
    clean: list[dict[str, str]] = []
    for item in refs:
        if not isinstance(item, dict) or set(item) - {"kind", "ref", "excerpt"}:
            raise ValueError("ref must be an object with kind, ref, excerpt only")
        kind, ref, excerpt = item.get("kind"), item.get("ref"), item.get("excerpt", "")
        if kind not in REF_KINDS:
            raise ValueError(f"ref kind must be one of {', '.join(REF_KINDS)}")
        if not isinstance(ref, str) or not 1 <= len(ref) <= MAX_REF_LEN:
            raise ValueError(f"ref must be a string of 1..{MAX_REF_LEN} chars")
        if not isinstance(excerpt, str) or len(excerpt) > MAX_EXCERPT_LEN:
            raise ValueError(f"excerpt must be a string of at most {MAX_EXCERPT_LEN} chars")
        clean.append({"kind": kind, "ref": ref, "excerpt": excerpt})
    return clean
