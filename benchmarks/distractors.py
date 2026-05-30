"""Generate synthetic distractor tools to inflate the tool universe.

The scaling experiment needs a tool catalog of arbitrary size N. We keep the
small set of *gold* tools a task actually needs and pad the rest with plausible
but irrelevant tools, so we can grow N from dozens to tens of thousands and watch
the in-context baseline break down while catalog+retrieval stays flat.

Distractors are deterministic given a seed so runs are reproducible.
"""
from __future__ import annotations

import random
from typing import Dict, List

_DOMAINS = [
    "inventory", "payroll", "logistics", "telemetry", "billing", "ticketing",
    "crm", "warehouse", "hr", "compliance", "marketing", "analytics", "iot",
    "fleet", "catalog", "loyalty", "procurement", "scheduling", "moderation",
    "provisioning", "metering", "escrow", "rostering", "dispatch", "valuation",
]
_VERBS = [
    "list", "create", "update", "delete", "sync", "export", "import", "audit",
    "reconcile", "archive", "validate", "summarize", "notify", "assign", "tag",
]
_OBJECTS = [
    "records", "accounts", "shipments", "invoices", "events", "tickets",
    "devices", "routes", "batches", "policies", "campaigns", "reports",
    "sensors", "vehicles", "entries", "members", "orders", "shifts",
]


def make_distractors(n: int, seed: int = 0, exclude: set = None) -> List[Dict[str, str]]:
    """Return n unique synthetic tools {name, description}, none in `exclude`."""
    exclude = exclude or set()
    rng = random.Random(seed)
    out: List[Dict[str, str]] = []
    seen = set(exclude)
    attempts = 0
    while len(out) < n and attempts < n * 50:
        attempts += 1
        d, v, o = rng.choice(_DOMAINS), rng.choice(_VERBS), rng.choice(_OBJECTS)
        name = f"{d}_{v}_{o}_api"
        if name in seen:
            continue
        seen.add(name)
        out.append({
            "name": name,
            "description": f"{v.capitalize()} {o} in the {d} system; "
                           f"manage {d} {o} and their {rng.choice(_OBJECTS)}.",
        })
    return out
