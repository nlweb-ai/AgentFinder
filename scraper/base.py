"""Shared helpers for catalog scrapers.

Emits catalog entries in the exact schema the in-memory search backend reads
(see code/search_backend.py and data/catalog/*.json):

    {
      "identifier": "urn:ai:<authority>:<...>",
      "displayName": str,
      "mediaType": "application/...",
      "url": str,
      "description": str,
      "tags": [str],            # optional
      "version": str,           # optional
      "metadata": {...}         # optional
    }
"""
import re
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

GITHUB_API = "https://api.github.com"


def slugify(name: str) -> str:
    """Mirror code/who_handler.py:_slugify so identifiers stay consistent."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "item"


def make_entry(
    *,
    authority: str,
    name_parts: List[str],
    display_name: str,
    media_type: str,
    url: str,
    description: str,
    tags: Optional[List[str]] = None,
    version: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build one catalog entry with a urn:ai identifier.

    The identifier is `urn:ai:<authority>:<slug>:<slug>...` from name_parts.
    """
    parts = ":".join(slugify(p) for p in name_parts if p)
    entry: Dict[str, Any] = {
        "identifier": f"urn:ai:{authority}:{parts}",
        "displayName": display_name.strip() or parts,
        "mediaType": media_type,
        "url": url,
        "description": (description or "").strip(),
    }
    if tags:
        entry["tags"] = tags
    if version:
        entry["version"] = version
    if metadata:
        entry["metadata"] = metadata
    return entry


def dedupe(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop entries with duplicate identifiers, keeping first seen."""
    seen = set()
    out = []
    for e in entries:
        ident = e.get("identifier")
        if not ident or ident in seen:
            continue
        seen.add(ident)
        out.append(e)
    return out


def first_paragraph(markdown: str, limit: int = 500) -> str:
    """Best-effort short description from a README/SKILL.md.

    Prefers a YAML frontmatter `description:`, else the first non-heading,
    non-badge text line.
    """
    text = markdown or ""
    fm = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if fm:
        m = re.search(r"^description:\s*(.+)$", fm.group(1), re.MULTILINE)
        if m:
            return m.group(1).strip().strip('"\'')[:limit]
        text = text[fm.end():]
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!["):
            continue
        line = re.sub(r"[*_`>]", "", line)
        if line:
            return line[:limit]
    return ""


def github_headers(token: Optional[str]) -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def fetch_json(session: aiohttp.ClientSession, url: str, **kwargs) -> Any:
    async with session.get(url, **kwargs) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_text(session: aiohttp.ClientSession, url: str, **kwargs) -> str:
    async with session.get(url, **kwargs) as resp:
        if resp.status != 200:
            return ""
        return await resp.text()


async def github_list_dir(
    session: aiohttp.ClientSession,
    repo: str,
    path: str,
    ref: str,
    token: Optional[str],
) -> List[Dict[str, Any]]:
    """Return the GitHub contents listing for repo/path (non-recursive)."""
    url = f"{GITHUB_API}/repos/{repo}/contents/{path.strip('/')}"
    try:
        listing = await fetch_json(
            session, url, params={"ref": ref}, headers=github_headers(token)
        )
    except Exception as e:
        print(f"  GitHub listing failed for {repo}/{path}: {e}")
        return []
    return listing if isinstance(listing, list) else [listing]


def write_catalog(out_dir: str, filename: str, entries: List[Dict[str, Any]]) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)
    return path
