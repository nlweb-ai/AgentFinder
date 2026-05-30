"""Shared data types and the pluggable-component contracts.

Two things are pluggable, both resolved the same way (registry name or
`path/to/file.py:factory`):

  * Agent      — the coding/tool-use agent under test (chosen by whoever runs).
  * Benchmark  — the task suite + its success check.

Keeping both behind a tiny Protocol means the runner never imports a concrete
agent or benchmark; it only sees these interfaces.
"""
from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Protocol, Tuple, runtime_checkable


@dataclass
class Task:
    """One benchmark item the agent must solve."""
    id: str
    prompt: str
    workspace: Optional[str] = None          # working dir the benchmark prepared, if any
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Attempt:
    """What an agent produced for a Task."""
    task_id: str
    output: str = ""                          # final answer / patch / transcript tail
    artifacts: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None               # set if the agent run itself blew up


@dataclass
class Toolset:
    """Capabilities handed to the agent for a single attempt.

    `default_tools` is the static set the agent ships with — present in BOTH
    arms, because the comparison is "default tools" vs "default tools + Agent
    Finder discovery". `finder` is None in the control arm and a FinderClient in
    the treatment arm; the treatment claim is that discovery beats the defaults
    alone. How an agent surfaces either is the adapter's business — the harness
    only decides what's present.
    """
    default_tools: list = field(default_factory=list)
    finder: Optional["FinderLike"] = None
    seed: int = 0
    # Optional stubbed tool executor used by multi-turn agents: given a tool name
    # and args it returns (observation, solved). `solved` marks that the call
    # actually accomplished the task (the gold capability ran). None => the agent
    # cannot execute tools and must answer single-shot.
    execute: Optional[Callable[[str, Dict[str, Any]], Tuple[str, bool]]] = None


@runtime_checkable
class FinderLike(Protocol):
    def search(self, query: str, page_size: int = 5) -> list: ...


@runtime_checkable
class Agent(Protocol):
    name: str
    def solve(self, task: Task, tools: Toolset) -> Attempt: ...


@runtime_checkable
class Benchmark(Protocol):
    name: str
    def tasks(self) -> Iterable[Task]: ...
    def evaluate(self, task: Task, attempt: Attempt) -> bool: ...


# --- registries: built-in components register here at import time -------------

_AGENTS: Dict[str, Callable[..., Agent]] = {}
_BENCHMARKS: Dict[str, Callable[..., Benchmark]] = {}


def register_agent(name: str):
    def deco(factory: Callable[..., Agent]):
        _AGENTS[name] = factory
        return factory
    return deco


def register_benchmark(name: str):
    def deco(factory: Callable[..., Benchmark]):
        _BENCHMARKS[name] = factory
        return factory
    return deco


# --- loader: name in a registry, or "file.py:factory" / "module:factory" ------

def _load_spec(spec: str, registry: Dict[str, Callable], kind: str, **kwargs):
    if ":" in spec and (spec.endswith(".py") or "/" in spec.split(":")[0] or spec.split(":")[0].endswith(".py")):
        path_part, _, attr = spec.partition(":")
        p = Path(path_part).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"{kind} adapter file not found: {p}")
        mod_name = f"_bench_plugin_{p.stem}"
        sp = importlib.util.spec_from_file_location(mod_name, p)
        module = importlib.util.module_from_spec(sp)
        sp.loader.exec_module(module)  # type: ignore[union-attr]
        factory = getattr(module, attr)
    elif ":" in spec:
        mod_name, _, attr = spec.partition(":")
        module = importlib.import_module(mod_name)
        factory = getattr(module, attr)
    elif spec in registry:
        factory = registry[spec]
    else:
        raise KeyError(
            f"unknown {kind} {spec!r}. Registered: {sorted(registry)}. "
            f"Or pass a path like ./my_{kind}.py:factory"
        )
    return factory(**kwargs)


def load_agent(spec: str, **kwargs) -> Agent:
    # ensure built-ins are registered
    from . import agents  # noqa: F401
    return _load_spec(spec, _AGENTS, "agent", **kwargs)


def load_benchmark(spec: str, **kwargs) -> Benchmark:
    from . import suites  # noqa: F401
    return _load_spec(spec, _BENCHMARKS, "benchmark", **kwargs)


def list_agents() -> list:
    from . import agents  # noqa: F401
    return sorted(_AGENTS)


def list_benchmarks() -> list:
    from . import suites  # noqa: F401
    return sorted(_BENCHMARKS)
