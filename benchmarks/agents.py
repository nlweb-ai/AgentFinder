"""Built-in agent adapters.

These are reference adapters. Whoever runs the harness can pick one by name
(`--agent stub`, `--agent shell`) or supply their own with
`--agent path/to/my_agent.py:make_agent` — the factory just has to return an
object with `name` and `solve(task, tools) -> Attempt`.

The treatment arm hands the agent a `tools.finder`; an adapter that wants to
benefit from Agent Finder should query it (e.g. with the task prompt) and feed
the discovered augments into its own context. The control arm passes
`tools.finder is None`, so a well-written adapter degrades to "agent alone".
"""
from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .core import Agent, Attempt, Task, Toolset, register_agent


@register_agent("stub")
def make_stub(**_) -> Agent:
    """Zero-dependency agent that does no real work. Use it to smoke-test the
    harness wiring (loaders, A/B loop, reporting) without burning tokens. In the
    treatment arm it echoes the discovered augment names so you can confirm the
    finder is being reached."""

    class StubAgent:
        name = "stub"

        def solve(self, task: Task, tools: Toolset) -> Attempt:
            discovered = []
            if tools.finder is not None:
                try:
                    discovered = [
                        r.get("displayName") or r.get("identifier", "?")
                        for r in tools.finder.search(task.prompt, page_size=5)
                    ]
                except Exception as e:  # finder optional — never fail the arm on it
                    return Attempt(task.id, output="", error=f"finder error: {e}")
            return Attempt(
                task_id=task.id,
                output=f"[stub] prompt={task.prompt!r} augments={discovered}",
                artifacts={"discovered": discovered},
            )

    return StubAgent()


@register_agent("shell")
def make_shell(command: Optional[str] = None, **_) -> Agent:
    """Adapter that shells out to any external agent CLI.

    Configure the command via the `command` kwarg or the BENCH_AGENT_CMD env
    var. The task prompt is passed on stdin; if a finder is present, the rendered
    augment block is appended to the prompt under an `AVAILABLE AUGMENTS` header.
    Whatever the command writes to stdout becomes the attempt output. This lets
    someone benchmark Aider/OpenHands/Claude Code/etc. without writing Python —
    they wrap their CLI in one shell command.
    """
    cmd = command or os.getenv("BENCH_AGENT_CMD")
    if not cmd:
        raise ValueError("shell agent needs a command (kwarg `command` or env BENCH_AGENT_CMD)")

    class ShellAgent:
        name = "shell"

        def solve(self, task: Task, tools: Toolset) -> Attempt:
            prompt = task.prompt
            if tools.finder is not None:
                from .finder_tool import FinderClient
                try:
                    entries = tools.finder.search(task.prompt, page_size=5)
                    block = FinderClient.render_for_prompt(entries)
                    prompt = f"{prompt}\n\n## AVAILABLE AUGMENTS\n{block}\n"
                except Exception as e:
                    return Attempt(task.id, output="", error=f"finder error: {e}")
            try:
                proc = subprocess.run(
                    shlex.split(cmd),
                    input=prompt,
                    capture_output=True,
                    text=True,
                    cwd=task.workspace or None,
                    timeout=float(os.getenv("BENCH_AGENT_TIMEOUT", "1800")),
                )
            except subprocess.TimeoutExpired:
                return Attempt(task.id, output="", error="agent command timed out")
            return Attempt(
                task_id=task.id,
                output=proc.stdout,
                artifacts={"returncode": proc.returncode, "stderr": proc.stderr[-4000:]},
                error=None if proc.returncode == 0 else f"exit {proc.returncode}",
            )

    return ShellAgent()


@register_agent("llm")
def make_llm(model: Optional[str] = None, max_tokens: int = 1024, **_) -> Agent:
    """A single-shot LLM agent built on the project's own llm_backend (same
    Azure/OpenAI client pool and LLM_* env vars as Agent Finder). This is the
    reference subject agent for descriptions-only runs: in the treatment arm it
    queries Agent Finder with the task and pastes the discovered augment
    descriptions into its context before answering; in control it answers from
    the prompt alone. The delta between the two is the thing we're measuring.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))
    from llm_backend import get_llm_backend  # noqa: E402

    SYSTEM = (
        "You are a capable assistant solving a benchmark task. You are given a "
        "list of TOOLS (with descriptions) you can call. Decide which tool(s) "
        "best accomplish the task and name the exact tool(s) you would invoke. "
        "Answer directly and concisely."
    )

    def _render_tools(tools_list):
        lines = []
        for t in tools_list:
            name = t.get("name") or t.get("displayName") or t.get("identifier", "?")
            desc = (t.get("description") or "").strip().replace("\n", " ")
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines) if lines else "(none)"

    class LLMAgent:
        name = f"llm:{model or 'default'}"

        def __init__(self):
            # One persistent loop for the agent's lifetime so the backend's
            # httpx clients aren't bound to a loop that asyncio.run() would close.
            self._loop = asyncio.new_event_loop()
            self._backend = None

        async def _ensure(self):
            if self._backend is None:
                self._backend = get_llm_backend()
                await self._backend.initialize()
            return self._backend

        async def _solve_async(self, task: Task, tools: Toolset) -> Attempt:
            backend = await self._ensure()
            # The agent always sees its default (shipped) tools.
            available = list(tools.default_tools)
            discovered = []
            if tools.finder is not None:
                # Treatment: discover task-relevant tools beyond the defaults.
                try:
                    entries = tools.finder.search(task.prompt, page_size=5)
                    for e in entries:
                        available.append({
                            "name": e.get("displayName") or e.get("identifier", "?"),
                            "description": e.get("description", ""),
                        })
                    discovered = [a["name"] for a in available[len(tools.default_tools):]]
                except Exception as e:
                    return Attempt(task.id, output="", error=f"finder error: {e}")
            user = f"{task.prompt}\n\n## TOOLS\n{_render_tools(available)}\n"
            try:
                text, usage = await backend.generate(
                    [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
                    model=model, max_tokens=max_tokens,
                )
            except Exception as e:
                return Attempt(task.id, output="", error=f"llm error: {e}")
            return Attempt(task.id, output=text, artifacts={
                "discovered": discovered,
                "agent_tokens": usage["total_tokens"],
            })

        def solve(self, task: Task, tools: Toolset) -> Attempt:
            return self._loop.run_until_complete(self._solve_async(task, tools))

    return LLMAgent()


def _render_tool_lines(tools_list):
    lines = []
    for t in tools_list:
        name = t.get("name") or t.get("displayName") or t.get("identifier", "?")
        desc = (t.get("description") or "").strip().replace("\n", " ")
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines) if lines else "(none)"


@register_agent("loop")
def make_loop(model: Optional[str] = None, max_turns: int = 8,
              max_tokens: int = 512, page_size: int = 5, **_) -> Agent:
    """Multi-turn ReAct agent over a stubbed tool executor.

    This is the subject for the coverage/token experiment. It retrieves
    task-relevant tools (treatment), then loops: emit `CALL <tool>` or
    `FINAL <answer>`; the harness executes the call via `tools.execute` and feeds
    back an observation. When the needed tool is absent the agent thrashes on
    substitutes until it gives up or hits `max_turns` — that wasted trajectory is
    the token cost we measure. A successful call to the gold tool ends the run.
    Total tokens = sum of usage over every turn.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))
    from llm_backend import get_llm_backend  # noqa: E402

    SYSTEM = (
        "You are an agent that solves a task by calling tools. On each turn reply "
        "with EXACTLY ONE line, no other text:\n"
        "  CALL <tool_name> <optional json args>   -- to invoke a tool\n"
        "  FINAL <answer>                           -- when the task is done\n"
        "Only call tools from the AVAILABLE TOOLS list. After each CALL you get an "
        "OBSERVATION. Use the single tool that actually does the job; do not guess "
        "wildly. If no available tool can do it, say so in FINAL."
    )

    def _parse(line: str):
        line = line.strip()
        for tag in ("CALL", "FINAL"):
            if line.upper().startswith(tag):
                return tag, line[len(tag):].strip()
        return "FINAL", line  # model broke protocol -> treat as final answer

    class LoopAgent:
        name = f"loop:{model or 'default'}"

        def __init__(self):
            self._loop = asyncio.new_event_loop()
            self._backend = None

        async def _ensure(self):
            if self._backend is None:
                self._backend = get_llm_backend()
                await self._backend.initialize()
            return self._backend

        async def _solve_async(self, task: Task, tools: Toolset) -> Attempt:
            backend = await self._ensure()
            available = list(tools.default_tools)
            discovered = []
            if tools.finder is not None:
                try:
                    for e in tools.finder.search(task.prompt, page_size=page_size):
                        available.append({
                            "name": e.get("displayName") or e.get("identifier", "?"),
                            "description": e.get("description", ""),
                        })
                    discovered = [a["name"] for a in available[len(tools.default_tools):]]
                except Exception as e:
                    return Attempt(task.id, output="", error=f"finder error: {e}")
            names = {a.get("name") or a.get("displayName") for a in available}

            messages = [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content":
                    f"TASK: {task.prompt}\n\n## AVAILABLE TOOLS\n{_render_tool_lines(available)}\n"},
            ]
            total_tokens, calls, solved, output = 0, 0, False, ""
            for _turn in range(max_turns):
                try:
                    text, usage = await backend.generate(
                        messages, model=model, max_tokens=max_tokens)
                except Exception as e:
                    return Attempt(task.id, output="", error=f"llm error: {e}")
                total_tokens += usage["total_tokens"]
                first = next((ln for ln in text.splitlines() if ln.strip()), text)
                tag, rest = _parse(first)
                if tag == "FINAL":
                    output = rest
                    break
                calls += 1
                # Resolve against known names: tool names can contain spaces
                # (e.g. "GitDealFlow Signal Agent"), so match the longest known
                # name the CALL argument starts with before falling back to the
                # first whitespace-delimited token.
                arg = rest.strip().strip('"')
                tool = next(
                    (n for n in sorted(names, key=len, reverse=True)
                     if n and (arg == n or arg.startswith(n + " "))),
                    (arg.split()[0] if arg.split() else ""),
                )
                if tool not in names:
                    obs = f"OBSERVATION: no tool named '{tool}' is available."
                elif tools.execute is not None:
                    obs_text, did = tools.execute(tool, {"raw": rest})
                    obs = f"OBSERVATION: {obs_text}"
                    if did:
                        solved, output = True, f"[solved via {tool}] {obs_text}"
                        break
                else:
                    obs = "OBSERVATION: (no executor configured)"
                messages.append({"role": "assistant", "content": first})
                messages.append({"role": "user", "content": obs})
            return Attempt(task.id, output=output, artifacts={
                "discovered": discovered,
                "agent_tokens": total_tokens,
                "turns": _turn + 1,
                "calls": calls,
                "solved": solved,
            })

        def solve(self, task: Task, tools: Toolset) -> Attempt:
            return self._loop.run_until_complete(self._solve_async(task, tools))

    return LoopAgent()


def _parse3(line: str):
    s = line.strip()
    for tag in ("FIND", "CALL", "FINAL"):
        if s.upper().startswith(tag):
            return tag, s[len(tag):].strip()
    return "FINAL", s  # broke protocol -> treat as final answer


@register_agent("discover")
def make_discover(model: Optional[str] = None, max_turns: int = 8,
                  max_tokens: int = 512, page_size: int = 5, **_) -> Agent:
    """ReAct agent that must DECIDE to use Agent Finder, then DECIDE to use what
    it returns — the agency the `loop` agent skips by pre-fetching results.

    Agent Finder is exposed as a first-class action (`FIND <query>`) rather than
    pre-loaded into context. The agent sees only its shipped `default_tools`; if
    none fit it can search the catalog, and the tools Finder returns become
    callable on later turns. A STUBBED executor stands in for the real app (gold
    tool solves, others return nothing), so nothing logs into a third-party
    service. We record the behavioral funnel per task:

      * called_finder      — did the agent invoke FIND at all?
      * used_discovered    — did it then CALL a tool Finder surfaced?
      * solved             — was that the gold tool (task done)?

    This separates "the right tool was reachable" from "the agent chose to reach
    for it and chose to use it" — the thing a real Copilot/Codex/Claude Code does
    or doesn't do when Agent Finder is registered as an MCP tool.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "code"))
    from llm_backend import get_llm_backend  # noqa: E402

    SYSTEM = (
        "You solve a task by calling tools. Reply with EXACTLY ONE line, no other "
        "text:\n"
        "  FIND <query>            -- search Agent Finder for a tool that can help\n"
        "  CALL <tool_name> <args> -- invoke a tool you can see\n"
        "  FINAL <answer>          -- when the task is done (or truly impossible)\n"
        "You start with only the tools in AVAILABLE TOOLS. If none of them can do "
        "the task, use FIND to discover one from the wider catalog, then CALL the "
        "tool it returns. After each action you get an OBSERVATION. Do not invent "
        "tool names; only CALL tools you can see or that FIND returned."
    )

    class DiscoverAgent:
        name = f"discover:{model or 'default'}"

        def __init__(self):
            self._loop = asyncio.new_event_loop()
            self._backend = None

        async def _ensure(self):
            if self._backend is None:
                self._backend = get_llm_backend()
                await self._backend.initialize()
            return self._backend

        async def _solve_async(self, task: Task, tools: Toolset) -> Attempt:
            backend = await self._ensure()
            available = list(tools.default_tools)
            names = {a.get("name") or a.get("displayName") for a in available if a}
            discovered_names = set()
            finder_calls, calls, solved, output = 0, 0, False, ""
            used_discovered = False

            finder_line = ("\nYou also have FIND to search Agent Finder.\n"
                           if tools.finder is not None else "")
            messages = [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content":
                    f"TASK: {task.prompt}\n{finder_line}\n## AVAILABLE TOOLS\n"
                    f"{_render_tool_lines(available)}\n"},
            ]
            total_tokens = 0
            for _turn in range(max_turns):
                try:
                    text, usage = await backend.generate(
                        messages, model=model, max_tokens=max_tokens)
                except Exception as e:
                    return Attempt(task.id, output="", error=f"llm error: {e}")
                total_tokens += usage["total_tokens"]
                first = next((ln for ln in text.splitlines() if ln.strip()), text)
                tag, rest = _parse3(first)

                if tag == "FINAL":
                    output = rest
                    break

                if tag == "FIND":
                    obs = "OBSERVATION: Agent Finder is not available."
                    if tools.finder is not None:
                        finder_calls += 1
                        try:
                            hits = tools.finder.search(rest or task.prompt, page_size=page_size)
                        except Exception as e:
                            return Attempt(task.id, output="", error=f"finder error: {e}")
                        new = []
                        for e in hits:
                            nm = e.get("displayName") or e.get("identifier", "?")
                            names.add(nm)
                            discovered_names.add(nm)
                            new.append({"name": nm, "description": e.get("description", "")})
                        available += new
                        obs = ("OBSERVATION: Agent Finder returned these tools you can now CALL:\n"
                               + (_render_tool_lines(new) if new else "(none)"))
                    messages.append({"role": "assistant", "content": first})
                    messages.append({"role": "user", "content": obs})
                    continue

                # tag == CALL
                calls += 1
                arg = rest.strip().strip('"')
                tool = next(
                    (n for n in sorted(names, key=len, reverse=True)
                     if n and (arg == n or arg.startswith(n + " "))),
                    (arg.split()[0] if arg.split() else ""),
                )
                if tool not in names:
                    obs = f"OBSERVATION: no tool named '{tool}' is available."
                elif tools.execute is not None:
                    if tool in discovered_names:
                        used_discovered = True
                    obs_text, did = tools.execute(tool, {"raw": rest})
                    obs = f"OBSERVATION: {obs_text}"
                    if did:
                        solved, output = True, f"[solved via {tool}] {obs_text}"
                        messages.append({"role": "assistant", "content": first})
                        messages.append({"role": "user", "content": obs})
                        break
                else:
                    obs = "OBSERVATION: (no executor configured)"
                messages.append({"role": "assistant", "content": first})
                messages.append({"role": "user", "content": obs})

            return Attempt(task.id, output=output, artifacts={
                "called_finder": finder_calls > 0,
                "finder_calls": finder_calls,
                "used_discovered": used_discovered,
                "discovered": sorted(discovered_names),
                "solved": solved,
                "agent_tokens": total_tokens,
                "turns": _turn + 1,
                "calls": calls,
            })

        def solve(self, task: Task, tools: Toolset) -> Attempt:
            return self._loop.run_until_complete(self._solve_async(task, tools))

    return DiscoverAgent()
