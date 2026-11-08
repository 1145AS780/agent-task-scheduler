# Agent Task Scheduler

*the scheduler your agent can steer*

<p align="center">
  <img src=".github/assets/banner-ats.svg" alt="Agent Task Scheduler" width="100%">
</p>

**English** | [中文](README.zh-CN.md)

A purely local, daemon-free task scheduler for AI agents. Commands are split into three entity layers, the filesystem is the single source of truth, and the agent advances state between turns.

- No external services, no third-party libraries — Python 3 standard library only
- All state lives in `ATS_ROOT` (default `<cwd>/.ats`): plain text, editable by hand, versionable, backup-friendly
- Every command prints JSON so an agent can parse it; exit codes are stable
- Zero background processes. Nothing runs unless the agent (or a human) asks it to

## Why not a cron job

| | cron / systemd timer | Agent Task Scheduler |
| --- | --- | --- |
| Who starts it | A human edits crontab | The agent, inside a conversation turn |
| Who can steer it mid-flight | Nobody; wait or kill | The agent polls status each turn and re-decides |
| Output shape | Log text | JSON, stable exit codes |
| High-risk commands | No gate | L0-L3 with expiring approvals |

## When to use it

**Fits**

- Long commands that should not hold the conversation turn: training runs, builds, batch transcoding, long test suites
- High-risk commands (deletes, releases, touching production data) that need a human to approve first
- Also a good fit: repeated patrols or polling, ordered chains that skip downstream on failure, flaky work needing retry with backoff, post-hoc review of what ran and with what exit code, and batch downloads with resume/rate limiting (torrents via aria2c)

**Does not fit**

- Millisecond-scale atomic operations that must complete inside one transaction
- True distributed locking or multi-machine coordination — this is single-machine file-lock semantics only
- Second-accurate scheduling — the agent drives it by polling, so precision equals the polling interval
- Tamper-proof authentication — identity comes from config or environment variables and is declarative, not a security boundary

## Three layers

| Layer | Lives in | Responsibility | Lifetime |
| --- | --- | --- | --- |
| Task | `tasks/<task_id>/` | One command plus permission and timeout | One execution |
| Plan | `schedules/<sid>/plans/<pid>/` | One orchestration: multiple stages, each with multiple steps | One orchestration |
| Schedule | `schedules/<sid>/schedule.json` | Triggers a plan periodically or once | Long-lived |

Steps inside a stage may declare a single-level `depends_on`. Cross-stage ordering is expressed by stage boundaries: the next stage starts only after the previous one fully ends.

```
Schedule (periodic or one-shot)
  └─ Plan (attempt N, id = <sid>-a<N>)
       ├─ Stage s1 (serial | parallel)
       │    ├─ Step one  ──→ may depends_on peers in the same stage
       │    ├─ Step two
       │    └─ Step three
       └─ Stage s2
```

Every step is a task behind a permission gate, so it reuses the task layer's approval, events, and logging.

<p align="center">
  <img src="assets/ats-architecture.svg" alt="Three entity layers and the permission-gate checkpoint chain" width="100%">
</p>

## Requirements

- Python 3 (standard library only)
- `aria2c` on `PATH` — only if you want torrent and magnet downloads

## Quick start

```bash
# Create a task (permission level defaults to L0)
python scripts/ats.py create build --command "gradle build" --level L1

# Run it and wait
python scripts/ats.py run build --wait

# Inspect
python scripts/ats.py status build --json
python scripts/ats.py show build --lines 50
```

Real output of `status build --json` after the run above:

```json
{
  "task_id": "build",
  "status": "succeeded",
  "level": "L1",
  "exit_code": 0,
  "pid": 3744,
  "started_at": 1794112325,
  "ended_at": 1794112325,
  "approval_id": null
}
```

Plans, schedules, and downloads:

```bash
python scripts/ats_plan.py create --spec references/examples/plan-deps-retry.json
python scripts/ats_plan.py run <plan-id> --wait
python scripts/ats.py download --url https://example.com/file.bin --out ./dl --wait
python scripts/ats.py torrent --magnet "magnet:?xt=urn:btih:..." --out ./dl --wait
```

Local console and MCP:

```bash
# Web console on http://127.0.0.1:8765 (monitor, run, approvals, create)
python scripts/ats_ui.py --open

# Windows one-click: start in the background if needed, then open the browser
scripts\ats_ui.cmd <ATS_ROOT>
```

Both MCP transports are described in [MCP](#mcp) below.

## MCP

There are two ways to connect an MCP client. They share the same 16-tool table defined in `scripts/ats_mcp.py`; the HTTP endpoint imports it rather than duplicating it.

### stdio — the client launches the server

```bash
python scripts/ats_mcp.py [--root DIR]
```

stdio uses one JSON-RPC 2.0 message per line. stdout carries protocol traffic only; logs and tracebacks go to stderr. The adapter never imports the CLI modules — every tool call runs `ats.py` / `ats_plan.py` as a subprocess, so the filesystem stays the single source of truth. If either CLI file is missing, it exits with code `2`.

```json
{
  "mcpServers": {
    "agent-task-scheduler": {
      "command": "python",
      "args": ["<repo>/scripts/ats_mcp.py", "--root", "<ATS_ROOT>"]
    }
  }
}
```

### HTTP — built into the console, off by default

The console can host a Streamable HTTP endpoint (`POST /mcp`) inside its own process. It does **not** listen until you enable it:

```bash
# Console on 127.0.0.1:8765; the MCP endpoint defaults to console port + 1 (8766)
python scripts/ats_ui.py --open

# Override the MCP port explicitly
python scripts/ats_ui.py --port 8765 --mcp-port 8766 --open
```

- Default port: console port + 1; `--mcp-port` overrides it (`--port` remains the console port)
- Endpoint: `http://127.0.0.1:<mcp-port>/mcp`
- Enable from the console UI, or `POST /api/action` with `{"action":"mcp.start","confirm":true}`; without `confirm:true` the API returns HTTP 400. **Enabling it grants any local process the ability to execute commands**
- States: `stopped` / `running` / `failed` (the UI shows the failure reason, e.g. port already in use)
- `initialize` returns an `Mcp-Session-Id` response header; `DELETE /mcp` ends a session; idle sessions are pruned after 30 minutes
- `GET /mcp` returns 405 (no fake SSE stream); `GET /health` reports endpoint state; notifications return 202 with an empty body
- Disabling it stops listening and releases the port; exiting the console does the same

```json
{
  "mcpServers": {
    "agent-task-scheduler": {
      "type": "http",
      "url": "http://127.0.0.1:8766/mcp"
    }
  }
}
```

**Protocol**: the server supports `2025-06-18`, `2025-03-26`, and `2024-11-05`; `initialize` echoes the requested version when supported, otherwise the newest (`2025-06-18`). The HTTP endpoint reports `2025-06-18`.

**Tools (16)**: `task_create`, `task_run`, `task_status`, `task_list`, `task_logs`, `task_stop`, `task_check`, `approval_resolve`, `plan_list`, `plan_show`, `plan_run`, `plan_resume`, `schedule_check`, `schedule_pause`, `schedule_resume`, `schedule_cancel`.

**Errors**: JSON-RPC `-32700` parse error, `-32600` invalid request, `-32601` unknown method, `-32602` unknown tool or bad/missing params. Tool-level failures (unknown task, non-zero CLI exit) are returned as a normal result with `isError: true` — not as a protocol error — and internal handling exceptions become an `isError: true` result with `{"error":"internal_error"}` (traceback goes to stderr). HTTP adds `403` (bad Host or Origin), `415` (wrong Content-Type), `406` (unacceptable Accept), `413` (body over 8 MB), `404` (bad path or expired session), `405` (`GET /mcp`), `503` (endpoint not running).

**Security**: binds `127.0.0.1` only, regardless of the console `--host`; accepts only `127.0.0.1:<port>` or `localhost:<port>` in `Host`; requires a same-origin `Origin` when the header is present; sends no CORS headers. There is no token or account authentication — the loopback bind and explicit confirmation are the only guards.

## Permission tiers

| Level | Meaning | Approval |
| --- | --- | --- |
| L0 | Read-only and harmless | Never |
| L1 | Local writes, builds | Policy-dependent |
| L2 | Network, package installs | Usually required |
| L3 | Deletes, releases, production data | Always required |

Approvals have an expiry, support batch approval, and honour a whitelist. A high-risk command without a valid approval exits with code 4.

Exit codes: `0` success, `2` bad arguments or missing confirmation, `3` missing object or state conflict, `4` insufficient permission.

## Repository layout

```
SKILL.md                    Entry point: triggers and the full work loop
references/                 Specs loaded on demand
├── orchestration-spec.md   Stages, steps, dependencies, failure propagation
├── permissions.md          L0-L3 tiers, approval flow, whitelist
├── data-model.md           Every field of task/plan/schedule records
├── storage-layout.md       Directory tree under ATS_ROOT
├── state-machine.md        State transitions and terminal states
├── error-taxonomy.md       Error classes and the retry matrix
├── command-guide.md        How to write and invoke commands
├── anti-patterns.md        Common misuse and why it breaks
└── examples/               Runnable minimal samples
scripts/                    The scheduler itself
├── ats.py                  Task layer: create, run, download, torrent, approvals
├── ats_plan.py             Plan and schedule layer
├── ats_download.py         HTTP downloader with resume and integrity check
├── ats_torrent.py          aria2c wrapper for torrents and magnets
├── ats_mcp.py              MCP adapter (stdio, 16 tools)
├── ats_mcp_http.py         Streamable HTTP endpoint hosted by the console
├── ats_ui.py               Local web console: monitor, run, approvals, create
└── ats_ui.cmd              Windows one-click launcher (--ensure, then open browser)
tests/                      Checks that CI and contributors both run
├── lint.py                 Compile, indentation, whitespace, final newline
└── selftest.py             Behavioural cases for tasks, plans, gates, recovery
```

## Conventions

- `SKILL.md` is the only entry point; the YAML `description` decides when the skill triggers, the body routes to deeper documents
- `references/` is plural and flat — topic files sit directly beneath it, no further nesting
- `scripts/` holds executables only, no prose; scripts resolve their own paths and never hardcode absolute ones
- Samples live under `references/examples/` because they are documentation, not tooling
- No build artifacts: `__pycache__/`, `.ats/`, logs, and caches stay out

## Notes

- The scheduler writes nothing outside `ATS_ROOT`. Deleting that directory resets all state.
- State is plain files by design: you can inspect, edit, diff, and back it up without special tooling.
- Paths in the documents use placeholders such as `<cwd>`; they do not correspond to any specific project.

## Contributing

Run both checks locally before opening a pull request:

```bash
python tests/lint.py       # compile, indentation, whitespace, final newline
python tests/selftest.py   # tasks, plans, permission gates, supervisor recovery
```

Both scripts are standard library only, so there is nothing to install. Each test
case runs in its own temporary `ATS_ROOT` and cleans up after itself.

CI runs these same two commands on Linux, macOS, and Windows across Python 3.9 and
3.13. The `lint` job gates the `test` matrix, so a syntax or style failure stops the
pipeline before any behaviour test runs.

When you fix a bug or add behaviour, add a case to `tests/selftest.py`. The suite is
the contract: a fix without a regression test is an unverified claim.

## License

Released under the [MIT License](LICENSE). Free to use, modify, and redistribute, provided the copyright notice is retained.