"""Built-in benchmark suites.

A Benchmark yields Tasks and judges Attempts. Real suites (AppWorld, tau-bench,
SWE-bench, the MCP benchmarks, ...) are added as their own adapter files and
selected with `--benchmark path/to/appworld.py:make_benchmark`; this module
ships only `sample`, a tiny self-contained suite so the harness runs end-to-end
with no external downloads.

Adding a real suite means writing a factory that returns an object with:
    name: str
    tasks() -> Iterable[Task]
    evaluate(task, attempt) -> bool
The adapter owns dataset download, workspace prep, and the success check (e.g.
running the benchmark's own grader on the agent's patch/answer).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

from .core import Attempt, Benchmark, Task, register_benchmark


@register_benchmark("sample")
def make_sample(**_) -> Benchmark:
    """A handful of discovery-flavored tasks whose answer is a capability the
    catalog should surface. Success = the agent's output mentions the expected
    augment keyword. This is a wiring/plumbing check, NOT a real measurement."""

    items = [
        ("weather", "I need current weather conditions for a city.", "weather"),
        ("ppt", "Create a PowerPoint presentation from an outline.", "powerpoint"),
        ("pg", "Query a Postgres database and summarize a table.", "postgres"),
        ("browser", "Take a screenshot of a web page and read the DOM.", "browser"),
        ("github", "Open a pull request and comment on an issue.", "github"),
    ]

    class SampleBenchmark:
        name = "sample"

        def tasks(self) -> Iterable[Task]:
            for tid, prompt, kw in items:
                yield Task(id=tid, prompt=prompt, metadata={"expect": kw})

        def evaluate(self, task: Task, attempt: Attempt) -> bool:
            if attempt.error:
                return False
            return task.metadata.get("expect", "").lower() in (attempt.output or "").lower()

    return SampleBenchmark()


@register_benchmark("toolretrieval")
def make_toolretrieval(tasks: str = "", **_) -> Benchmark:
    """Tool-selection suite: each task names a capability that exactly one tool
    in a known pool provides; success = the agent's answer names the gold tool.

    This is the canonical descriptions-only experiment. Ingest the tool pool
    into Agent Finder first (so it can be discovered):

        python -m benchmarks.ingest \\
            --in benchmarks/data/toolretrieval/tools.json \\
            --out data/bench_toolretrieval --authority toolbench
        SEARCH_PROVIDER=memory AGENT_FINDER_CATALOG_DIR=data/bench_toolretrieval \\
            WHO_SERVER_PORT=8090 python code/agent_finder.py

    Then control = agent picks from its own knowledge (no pool shown); treatment
    = agent gets Finder's retrieved candidates and should name the gold tool. The
    delta isolates retrieval quality. Point `tasks` at a full ToolBench/ToolRet
    export (same {id, prompt, gold} shape) to scale up.
    """
    src = tasks or os.getenv("BENCH_TR_TASKS") or str(
        Path(__file__).resolve().parent / "data" / "toolretrieval" / "tasks.jsonl")
    fp = Path(src).expanduser().resolve()
    if not fp.exists():
        raise FileNotFoundError(f"toolretrieval tasks file not found: {fp}")

    class ToolRetrievalBenchmark:
        name = "toolretrieval"

        def tasks(self) -> Iterable[Task]:
            for line in fp.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                yield Task(id=str(obj["id"]), prompt=obj["prompt"],
                           metadata={"gold": obj["gold"]})

        def evaluate(self, task: Task, attempt: Attempt) -> bool:
            if attempt.error:
                return False
            return task.metadata.get("gold", "").lower() in (attempt.output or "").lower()

    return ToolRetrievalBenchmark()


@register_benchmark("appworld")
def make_appworld(split: str = "test_normal", limit: int = 0, **_) -> Benchmark:
    """AppWorld adapter (descriptions-only).

    Requires the `appworld` package and its downloaded data:
        pip install appworld && appworld download data
    Ingest AppWorld's app/API surface into Agent Finder so the treatment arm can
    retrieve the relevant apps for each task. This adapter wraps AppWorld's task
    iterator and its own evaluator; it does NOT reimplement grading.

    Kept import-light: the heavy dependency is imported lazily so the rest of the
    harness loads without AppWorld installed.
    """
    try:
        from appworld import AppWorld, load_task_ids  # type: ignore
    except ImportError as e:
        raise ImportError(
            "appworld not installed. Run `pip install appworld` and "
            "`appworld download data`, then ingest its API surface into Agent "
            "Finder before benchmarking. See benchmarks/README.md."
        ) from e

    task_ids = load_task_ids(split)
    if limit:
        task_ids = task_ids[:limit]

    class AppWorldBenchmark:
        name = f"appworld:{split}"

        def tasks(self) -> Iterable[Task]:
            for tid in task_ids:
                with AppWorld(task_id=tid) as world:
                    yield Task(id=tid, prompt=world.task.instruction,
                               metadata={"task_id": tid})

        def evaluate(self, task: Task, attempt: Attempt) -> bool:
            # AppWorld grades the world state the agent mutated, not free text.
            # An adapter that drives AppWorld's API must record the world handle
            # in attempt.artifacts["world"]; we defer to AppWorld's evaluator.
            world = attempt.artifacts.get("world")
            if world is None:
                return False
            return bool(world.evaluate().success)

    return AppWorldBenchmark()


@register_benchmark("jsonl")
def make_jsonl(path: str = "", **_) -> Benchmark:
    """Generic file-backed suite: one JSON object per line with at least
    `id` and `prompt`, optionally `expect` (substring success check) and
    `workspace`. Lets someone drop in their own task list without writing code.
    Path via the `path` kwarg or BENCH_JSONL env var."""

    src = path or os.getenv("BENCH_JSONL", "")
    if not src:
        raise ValueError("jsonl benchmark needs a path (kwarg `path` or env BENCH_JSONL)")
    fp = Path(src).expanduser().resolve()
    if not fp.exists():
        raise FileNotFoundError(f"jsonl task file not found: {fp}")

    class JsonlBenchmark:
        name = f"jsonl:{fp.name}"

        def tasks(self) -> Iterable[Task]:
            for line in fp.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                yield Task(
                    id=str(obj["id"]),
                    prompt=obj["prompt"],
                    workspace=obj.get("workspace"),
                    metadata={k: v for k, v in obj.items() if k not in ("id", "prompt", "workspace")},
                )

        def evaluate(self, task: Task, attempt: Attempt) -> bool:
            if attempt.error:
                return False
            expect = task.metadata.get("expect")
            if expect is None:
                # No grader provided — count a non-empty, error-free run as pass.
                return bool((attempt.output or "").strip())
            return str(expect).lower() in (attempt.output or "").lower()

    return JsonlBenchmark()
