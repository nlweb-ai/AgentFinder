"""A/B benchmark harness: does coding-agent + Agent Finder beat coding-agent alone?

The harness is agent-agnostic. Whoever runs it chooses the subject agent with
`--agent <name>` (a registered adapter) or `--agent path/to/file.py:factory`
(their own). The same task set is run in two conditions — control (agent alone)
and treatment (agent + Agent Finder discovery) — and the delta in task success
is reported.
"""
