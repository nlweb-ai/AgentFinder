"""Dedup pass: collapse redundant catalog entries (the GitHub-MCP-server clone
problem) down to one authentic entry per real tool.

The scraped catalog lists the same underlying tool many times — a dozen
independent "GitHub MCP server" reimplementations, each from a different registry
or author, all doing repo/issue/PR management. They bloat the index, split
retrieval signal across siblings, and let a reranker surface a low-quality fork
over the canonical one. We want to keep ONE authentic GitHub repo server while
still keeping functionally DISTINCT GitHub tools (a stale-branch health monitor,
a read-only repo explainer).

Why an LLM judge and not an embedding threshold? Measured on the real catalog,
the cosine between two generic GitHub servers (~0.55-0.69) is indistinguishable
from the cosine between a generic server and a genuinely different GitHub tool
(~0.60-0.65). No distance threshold separates "redundant clone" from "distinct
same-domain tool", and the clones share no canonical repo URL to key on. So we
let a model make the equivalence call.

Pipeline: cheap name-relatedness bucketing groups candidates that *might* be the
same tool (only popular names form big buckets); the LLM judges each bucket,
returning which entries are mutually redundant. Per redundant group we keep the
most authentic entry (best-documented / canonical identifier) and drop the rest.

Run BEFORE enrich.py so tokens aren't spent enriching clones you discard:
    source ./set_keys.sh
    python -m scraper.dedup --in data/catalog_5k --out data/catalog_dedup
    python -m scraper.enrich --in data/catalog_dedup --out data/catalog_enriched2 \
        --model gpt-5.1 --concurrency 30

A human-auditable <out>/dedup_report.json lists every dropped group (keeper +
dropped + the model's reason) so you can eyeball the merges.
"""
import os
import re
import sys
import json
import asyncio
import argparse
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))
from llm_backend import get_llm_backend  # noqa: E402

from .enrich import load_catalog  # reuse the dedup/merge catalog loader

_WORD = re.compile(r"[a-z0-9]+")
# Generic tokens that shouldn't, on their own, make two tools "name-related".
STOP = {"mcp", "server", "agent", "api", "tool", "tools", "ai", "app", "service",
        "the", "for", "and", "of", "a", "an", "client", "skill"}

JUDGE_SYSTEM = (
    "You deduplicate a catalog of AI tools. You are given a numbered list of "
    "entries (name + description) that share a name and MIGHT be the same tool. "
    "Group entries that are REDUNDANT reimplementations of the SAME tool doing "
    "the SAME job (e.g. several generic 'GitHub repo/issue/PR management' "
    "servers). Keep functionally DISTINCT tools in their own group even if the "
    "name overlaps (e.g. a stale-branch health monitor, or a read-only repo "
    "explainer, are NOT the same as a generic GitHub server). Output STRICT JSON: "
    '{"groups": [[0,3,5], [1], [2,4]]}  -- each inner array lists the indices of '
    "entries that are mutually redundant. Every index must appear exactly once. "
    "Singletons (distinct tools) get their own one-element group. Output only JSON."
)


def _ident(e: Dict[str, Any]) -> str:
    return e.get("identifier") or e.get("url") or ""


def _name(e: Dict[str, Any]) -> str:
    return e.get("displayName") or e.get("name") or ""


def _tokens(e: Dict[str, Any]) -> set:
    return {t for t in _WORD.findall(_name(e).lower()) if t not in STOP}


def _all_tokens(e: Dict[str, Any]) -> set:
    """Tokens including the common ones, used for the relatedness test (so two
    'GitHub' entries are related even though 'github' is a non-bucketing token)."""
    drop = {"mcp", "server", "the", "for", "and", "of", "a", "an"}
    return {t for t in _WORD.findall(_name(e).lower()) if t not in drop}


def _authenticity(e: Dict[str, Any]) -> Tuple:
    """Higher tuple sorts first => kept: best-documented, most metadata, has url,
    then shortest identifier (canonical 'github' beats a long fork id)."""
    desc = e.get("description") or ""
    meta = e.get("metadata") or {}
    return (len(desc), len(meta), 1 if e.get("url") else 0, -len(_ident(e)))


def name_related(a: set, b: set) -> bool:
    # Jaccard >= 0.5 only. A subset rule would chain a bare single-token name
    # ("GitHub" = {github}) into every multi-word tool that contains it
    # ("GitHub Health Monitor"), snowballing distinct tools into one mega-bucket.
    if not a or not b or not (a & b):
        return False
    return len(a & b) / len(a | b) >= 0.5


def build_buckets(entries: List[Dict[str, Any]], max_bucket: int) -> List[List[int]]:
    """Union-Find connected components over name-relatedness, restricted to
    entries sharing a distinctive (non-STOP) name token so we don't compare all
    pairs. Returns only multi-entry buckets within size cap."""
    n = len(entries)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    inv: Dict[str, List[int]] = {}
    for i, e in enumerate(entries):
        for tok in _tokens(e):
            inv.setdefault(tok, []).append(i)
    alltok = [_all_tokens(e) for e in entries]
    for tok, members in inv.items():
        if len(members) < 2 or len(members) > 400:  # skip pathologically common tokens
            continue
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                i, j = members[x], members[y]
                if name_related(alltok[i], alltok[j]):
                    union(i, j)

    groups: Dict[int, List[int]] = {}
    for k in range(n):
        groups.setdefault(find(k), []).append(k)
    buckets = [g for g in groups.values() if len(g) >= 2]
    over = [g for g in buckets if len(g) > max_bucket]
    if over:
        print(f"  {len(over)} buckets exceed --max-bucket={max_bucket} "
              f"(left intact): " + ", ".join(_name(entries[g[0]]) for g in over[:5]))
    return [g for g in buckets if len(g) <= max_bucket]


def _bucket_prompt(entries, bucket) -> str:
    lines = []
    for n, i in enumerate(bucket):
        e = entries[i]
        desc = (e.get("description") or "").strip().replace("\n", " ")[:300]
        lines.append(f"[{n}] {_name(e)} :: {desc}")
    return "\n".join(lines)


async def judge_buckets(entries, buckets, backend, model, concurrency):
    """Ask the LLM to split each bucket into redundant groups. Returns
    list of (bucket, groups_of_indices_into_bucket)."""
    sem = asyncio.Semaphore(concurrency)
    results: List[Tuple[List[int], List[List[int]]]] = [None] * len(buckets)

    async def one(bi, bucket):
        async with sem:
            try:
                text, _ = await backend.generate(
                    [{"role": "system", "content": JUDGE_SYSTEM},
                     {"role": "user", "content": _bucket_prompt(entries, bucket)}],
                    model=model, max_tokens=600)
                groups = _parse_groups(text, len(bucket))
            except Exception as ex:
                print(f"  judge error on bucket '{_name(entries[bucket[0]])}': {ex}")
                groups = [[k] for k in range(len(bucket))]  # fail safe: keep all
        results[bi] = (bucket, groups)

    await asyncio.gather(*(one(bi, b) for bi, b in enumerate(buckets)),
                         return_exceptions=True)
    return results


def _parse_groups(text: str, size: int) -> List[List[int]]:
    txt = (text or "").strip()
    if not txt.startswith("{"):
        i, j = txt.find("{"), txt.rfind("}")
        txt = txt[i:j + 1] if (i != -1 and j > i) else ""
    try:
        groups = json.loads(txt).get("groups", [])
    except (json.JSONDecodeError, AttributeError):
        return [[k] for k in range(size)]
    seen, clean = set(), []
    for g in groups:
        gg = [int(x) for x in g if isinstance(x, int) and 0 <= int(x) < size and int(x) not in seen]
        seen.update(gg)
        if gg:
            clean.append(gg)
    for k in range(size):  # any index the model dropped stays as its own keeper
        if k not in seen:
            clean.append([k])
    return clean


def apply_dedup(entries, judged) -> Tuple[set, List[Dict[str, Any]]]:
    """Returns (dropped_idents, report). Within each redundant group of >1, keep
    the most authentic entry and drop the others."""
    dropped, report = set(), []
    for bucket, groups in judged:
        for g in groups:
            if len(g) < 2:
                continue
            members = [bucket[k] for k in g]
            ranked = sorted(members, key=lambda i: _authenticity(entries[i]), reverse=True)
            keeper = entries[ranked[0]]
            for i in ranked[1:]:
                dropped.add(_ident(entries[i]))
            report.append({
                "keep": {"identifier": _ident(keeper), "name": _name(keeper)},
                "drop": [{"identifier": _ident(entries[i]), "name": _name(entries[i])}
                         for i in ranked[1:]],
            })
    return dropped, report


async def run(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    header, entries = load_catalog(args.in_dir)
    if args.limit:
        entries = entries[: args.limit]
    print(f"Loaded {len(entries)} entries from {args.in_dir}/")

    buckets = build_buckets(entries, args.max_bucket)
    n_cand = sum(len(b) for b in buckets)
    print(f"{len(buckets)} name-related buckets to judge ({n_cand} candidate entries).")

    judged: List[Tuple[List[int], List[List[int]]]] = []
    if buckets:
        backend = get_llm_backend()
        await backend.initialize()
        try:
            judged = await judge_buckets(entries, buckets, backend, args.model, args.concurrency)
        finally:
            await backend.close()

    dropped, report = apply_dedup(entries, judged)
    kept = [e for e in entries if _ident(e) not in dropped]

    manifest = {**header, "entries": kept}
    manifest.setdefault("specVersion", "1.0")
    (out / "ai-catalog.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "dedup_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"\nKept {len(kept)}/{len(entries)} entries; dropped {len(dropped)} "
        f"redundant clones across {len(report)} groups.\n"
        f"Wrote {out}/ai-catalog.json and {out}/dedup_report.json (audit the merges)."
    )


def main(argv=None):
    p = argparse.ArgumentParser(description="LLM-judged dedup of redundant catalog clones.")
    p.add_argument("--in", dest="in_dir", default="data/catalog_5k", help="Catalog dir to read.")
    p.add_argument("--out", default="data/catalog_dedup", help="Dir for deduped catalog + report.")
    p.add_argument("--model", default=None, help="Judge model (default: LLM_MODEL / backend default).")
    p.add_argument("--max-bucket", type=int, default=40, help="Skip name buckets larger than this.")
    p.add_argument("--concurrency", type=int, default=20, help="Max concurrent judge calls.")
    p.add_argument("--limit", type=int, default=0, help="Only consider the first N entries (dry sample).")
    args = p.parse_args(argv)

    for var in ("LLM_ENDPOINT", "LLM_API_KEY"):
        if not os.getenv(var):
            print(f"Warning: {var} not set — the LLM backend will likely fail to initialize.", file=sys.stderr)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
