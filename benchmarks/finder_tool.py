"""Client for a running Agent Finder instance, injected in the treatment arm.

Reuses the live `/search` HTTP endpoint (spec request shape:
`{"query": {"text": ...}, "pageSize": N}`) instead of importing the backend, so
the harness exercises Agent Finder exactly as a real agent would over the wire.
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any, Dict, List


class FinderClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8090", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def search(self, query: str, page_size: int = 5) -> List[Dict[str, Any]]:
        """Return spec §4.2 catalog entries most relevant to `query` (possibly empty)."""
        body = json.dumps({"query": {"text": query}, "pageSize": page_size}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/search",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Agent Finder search failed against {self.base_url}: {e}") from e
        return data.get("results", [])

    def health(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=self.timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    @staticmethod
    def render_for_prompt(entries: List[Dict[str, Any]]) -> str:
        """Format search results as a compact block an agent adapter can paste
        into its context. Adapters are free to ignore this and use the raw
        entries instead."""
        if not entries:
            return "(no relevant augments found)"
        lines = []
        for e in entries:
            name = e.get("displayName") or e.get("identifier", "?")
            typ = (e.get("type") or "").split("/")[-1]
            url = e.get("url", "")
            desc = (e.get("description") or "").strip().replace("\n", " ")
            if len(desc) > 240:
                desc = desc[:237] + "..."
            lines.append(f"- {name} [{typ}] {url}\n  {desc}")
        return "\n".join(lines)
