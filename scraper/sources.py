"""Trustworthy catalog sources.

Each fetcher is `async fn(session, token, limit) -> List[entry]` and returns
catalog entries built via base.make_entry. Add a new source by writing a
fetcher and registering it in SOURCES at the bottom.
"""
from typing import Any, Dict, List, Optional

import aiohttp

from . import base

RAW = "https://raw.githubusercontent.com"


# --------------------------------------------------------------------------
# MCP servers — the official Model Context Protocol registry
# https://registry.modelcontextprotocol.io
# --------------------------------------------------------------------------
async def fetch_mcp(session: aiohttp.ClientSession, token: Optional[str], limit: int) -> List[Dict[str, Any]]:
    base_url = "https://registry.modelcontextprotocol.io/v0/servers"
    authority = "registry.modelcontextprotocol.io"
    entries: List[Dict[str, Any]] = []
    cursor = None
    while len(entries) < limit:
        params = {"limit": "100"}
        if cursor:
            params["cursor"] = cursor
        try:
            data = await base.fetch_json(session, base_url, params=params)
        except Exception as e:
            print(f"  MCP registry fetch failed: {e}")
            break

        servers = data.get("servers") or data.get("data") or []
        for item in servers:
            srv = item.get("server", item) if isinstance(item, dict) else {}
            meta = item.get("_meta", {}) if isinstance(item, dict) else {}
            official = meta.get("io.modelcontextprotocol.registry/official", {})
            # Skip superseded versions; keep only the current published one.
            if official.get("isLatest") is False:
                continue
            name = srv.get("name") or ""
            if not name:
                continue
            repo = srv.get("repository") or {}
            remotes = srv.get("remotes") or []
            url = (
                (remotes[0].get("url") if remotes and isinstance(remotes[0], dict) else None)
                or repo.get("url")
                or srv.get("websiteUrl")
                or ""
            )
            # name is typically "io.github.owner/repo" — slugify the whole thing.
            display = srv.get("title") or name.split("/")[-1]
            entries.append(base.make_entry(
                authority=authority,
                name_parts=[name.replace("/", "-")],
                display_name=display,
                media_type="application/mcp-server+json",
                url=url,
                description=srv.get("description", ""),
                version=str(srv.get("version")) if srv.get("version") else None,
                metadata={"source": authority},
            ))

        meta = data.get("metadata") or {}
        cursor = meta.get("next_cursor") or meta.get("nextCursor")
        if not cursor or not servers:
            break
    return entries[:limit]


# --------------------------------------------------------------------------
# MCP servers — PulseMCP (~16k, hand-reviewed). Free, offset-paginated.
# https://www.pulsemcp.com/api/docs
# --------------------------------------------------------------------------
async def fetch_pulsemcp(session: aiohttp.ClientSession, token: Optional[str], limit: int) -> List[Dict[str, Any]]:
    api = "https://api.pulsemcp.com/v0beta/servers"
    authority = "pulsemcp.com"
    page = 100
    entries: List[Dict[str, Any]] = []
    offset = 0
    while len(entries) < limit:
        try:
            data = await base.fetch_json(session, api, params={"count_per_page": str(page), "offset": str(offset)})
        except aiohttp.ClientResponseError as e:
            # The free v0beta endpoint caps pagination depth (~200) with 410 Gone;
            # the full catalog needs an authenticated v0.1 key.
            if e.status == 410:
                print(f"  PulseMCP free endpoint cap reached at offset {offset} (full 16k+ set needs a v0.1 API key)")
            else:
                print(f"  PulseMCP fetch failed at offset {offset}: {e}")
            break
        servers = data.get("servers") or []
        if not servers:
            break
        for srv in servers:
            name = srv.get("name") or ""
            if not name:
                continue
            # The PulseMCP page URL ends in a stable unique slug — use it for the identifier.
            slug = (srv.get("url") or "").rstrip("/").split("/")[-1] or name
            url = srv.get("external_url") or srv.get("source_code_url") or srv.get("url") or ""
            desc = srv.get("short_description") or srv.get("EXPERIMENTAL_ai_generated_description") or ""
            meta = {"source": authority}
            if srv.get("github_stars") is not None:
                meta["github_stars"] = srv["github_stars"]
            if srv.get("package_name"):
                meta["package_name"] = srv["package_name"]
            if srv.get("package_registry"):
                meta["package_registry"] = srv["package_registry"]
            entries.append(base.make_entry(
                authority=authority,
                name_parts=[slug],
                display_name=name,
                media_type="application/mcp-server+json",
                url=url,
                description=desc,
                metadata=meta,
            ))
        offset += len(servers)
        if not data.get("next"):
            break
    return entries[:limit]


# --------------------------------------------------------------------------
# MCP servers — Glama registry (~27k). Free, cursor-paginated.
# https://glama.ai/mcp/servers
# --------------------------------------------------------------------------
async def fetch_glama(session: aiohttp.ClientSession, token: Optional[str], limit: int) -> List[Dict[str, Any]]:
    api = "https://glama.ai/api/mcp/v1/servers"
    authority = "glama.ai"
    entries: List[Dict[str, Any]] = []
    cursor = None
    while len(entries) < limit:
        params = {"first": "100"}
        if cursor:
            params["after"] = cursor
        try:
            data = await base.fetch_json(session, api, params=params)
        except Exception as e:
            print(f"  Glama fetch failed: {e}")
            break
        servers = data.get("servers") or []
        if not servers:
            break
        for srv in servers:
            namespace = srv.get("namespace") or ""
            slug = srv.get("slug") or srv.get("id") or ""
            if not slug:
                continue
            repo = srv.get("repository") or {}
            url = srv.get("url") or repo.get("url") or ""
            meta = {"source": authority}
            if srv.get("attributes"):
                meta["attributes"] = srv["attributes"]
            lic = srv.get("spdxLicense") or {}
            if lic.get("name"):
                meta["license"] = lic["name"]
            entries.append(base.make_entry(
                authority=authority,
                name_parts=[namespace, slug],
                display_name=srv.get("name") or slug,
                media_type="application/mcp-server+json",
                url=url,
                description=srv.get("description", ""),
                metadata=meta,
            ))
        page_info = data.get("pageInfo") or {}
        cursor = page_info.get("endCursor")
        if not page_info.get("hasNextPage") or not cursor:
            break
    return entries[:limit]


# --------------------------------------------------------------------------
# A2A agents — github.com/a2aproject/a2a-samples
# --------------------------------------------------------------------------
async def fetch_a2a(session: aiohttp.ClientSession, token: Optional[str], limit: int) -> List[Dict[str, Any]]:
    repo = "a2aproject/a2a-samples"
    ref = "main"
    authority = "github.com"
    base_path = "samples/python/agents"
    dirs = await base.github_list_dir(session, repo, base_path, ref, token)
    entries: List[Dict[str, Any]] = []
    for item in dirs:
        if item.get("type") != "dir" or len(entries) >= limit:
            continue
        name = item["name"]
        path = f"{base_path}/{name}"
        readme = await base.fetch_text(session, f"{RAW}/{repo}/{ref}/{path}/README.md")
        desc = base.first_paragraph(readme) or f"A2A sample agent: {name}"
        entries.append(base.make_entry(
            authority=authority,
            name_parts=["a2aproject", "a2a-samples", name],
            display_name=name.replace("_", " ").replace("-", " ").title(),
            media_type="application/a2a-agent-card+json",
            url=f"https://github.com/{repo}/tree/{ref}/{path}",
            description=desc,
            metadata={"framework": "A2A", "source": f"github.com/{repo}"},
        ))
    return entries


# --------------------------------------------------------------------------
# A2A agents — a2aregistry.org (live hosted agents, full agent cards)
# https://a2aregistry.org
# --------------------------------------------------------------------------
async def fetch_a2a_registry(session: aiohttp.ClientSession, token: Optional[str], limit: int) -> List[Dict[str, Any]]:
    api = "https://a2aregistry.org/api/agents"
    authority = "a2aregistry.org"
    entries: List[Dict[str, Any]] = []
    offset = 0
    page = 100
    while len(entries) < limit:
        try:
            data = await base.fetch_json(session, api, params={"limit": str(page), "offset": str(offset)})
        except Exception as e:
            print(f"  a2aregistry fetch failed at offset {offset}: {e}")
            break
        agents = data.get("agents") if isinstance(data, dict) else data
        if not agents:
            break
        for ag in agents:
            name = ag.get("name") or ""
            ident = ag.get("id") or name
            if not ident:
                continue
            skills = ag.get("skills") or []
            skill_names = [s.get("name", "") for s in skills if isinstance(s, dict)]
            provider = ag.get("provider") or {}
            meta = {"source": authority, "framework": "A2A"}
            if ag.get("author"):
                meta["author"] = ag["author"]
            if provider.get("organization"):
                meta["provider"] = provider["organization"]
            if ag.get("documentationUrl"):
                meta["documentationUrl"] = ag["documentationUrl"]
            entries.append(base.make_entry(
                authority=authority,
                name_parts=[str(ident)],
                display_name=name or str(ident),
                media_type="application/a2a-agent-card+json",
                url=ag.get("url") or ag.get("wellKnownURI") or ag.get("homepage") or "",
                description=ag.get("description", ""),
                tags=skill_names or None,
                version=str(ag.get("version")) if ag.get("version") else None,
                metadata=meta,
            ))
        total = data.get("total") if isinstance(data, dict) else None
        offset += len(agents)
        if total is not None and offset >= total:
            break
    return entries[:limit]


# --------------------------------------------------------------------------
# Skills — github.com/anthropics/skills + github.com/huggingface/skills
# --------------------------------------------------------------------------
async def _github_tree(session, repo, ref, token) -> List[Dict[str, Any]]:
    url = f"{base.GITHUB_API}/repos/{repo}/git/trees/{ref}"
    try:
        data = await base.fetch_json(
            session, url, params={"recursive": "1"}, headers=base.github_headers(token)
        )
    except Exception as e:
        print(f"  GitHub tree fetch failed for {repo}: {e}")
        return []
    return data.get("tree", [])


# First-party GitHub repos that publish skills as `<dir>/SKILL.md`.
SKILL_REPOS = ["anthropics/skills", "huggingface/skills"]


async def fetch_skills(session: aiohttp.ClientSession, token: Optional[str], limit: int) -> List[Dict[str, Any]]:
    ref = "main"
    authority = "github.com"
    entries: List[Dict[str, Any]] = []
    for repo in SKILL_REPOS:
        if len(entries) >= limit:
            break
        owner = repo.split("/")[0]
        tree = await _github_tree(session, repo, ref, token)
        skill_files = [t["path"] for t in tree if t.get("path", "").endswith("/SKILL.md")]
        for path in skill_files:
            if len(entries) >= limit:
                break
            skill_dir = path[: -len("/SKILL.md")]
            name = skill_dir.split("/")[-1]
            md = await base.fetch_text(session, f"{RAW}/{repo}/{ref}/{path}")
            desc = base.first_paragraph(md) or f"Agent skill: {name}"
            entries.append(base.make_entry(
                authority=authority,
                name_parts=[owner, "skills", name],
                display_name=name.replace("_", " ").replace("-", " ").title(),
                media_type="application/ai-skill",
                url=f"https://github.com/{repo}/tree/{ref}/{skill_dir}",
                description=desc,
                metadata={"source": f"github.com/{repo}"},
            ))
    return entries


SOURCES = {
    "pulsemcp": ("mcp_pulsemcp.json", fetch_pulsemcp),   # ~16k MCP servers
    "glama": ("mcp_glama.json", fetch_glama),            # ~27k MCP servers
    "mcp": ("mcp_registry.json", fetch_mcp),             # ~2k official registry
    "a2a": ("a2a_samples.json", fetch_a2a),
    "a2a_registry": ("a2a_registry.json", fetch_a2a_registry),  # live hosted agents
    "skills": ("skills.json", fetch_skills),
}
