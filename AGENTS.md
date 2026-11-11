# AGENTS.md

Guidance for agents (and people) working **on** this repository. For how an agent *uses* the
server, read `src/hypershell_mcp/guide.md`, which the server also serves as `hypershell://guide`.

## What this is

`hypershell-mcp` is an MCP server for [HyperShell](https://hypershell.org), built on FastMCP 4 and
HyperShell 2.9. `README.md` is the user's view; `DESIGN.md` records the decisions and what is open.

## Layout

```
src/hypershell_mcp/
  auth.py        Tiers and scopes, the JWT verifier, minting tokens and keys
  settings.py    Settings from the [mcp] config section and CLI overrides; what may be served
  backend.py     Every HyperShell database read and write, on one thread (no MCP in here)
  clusters.py    Starting, finding and stopping detached `hs cluster` processes
  outputs.py     Bounded, confined reading of captured task output
  diagnose.py    Grouping failures by exit status and masked stderr signature
  templates.py   Text-only submit templates (the safe subset of HyperShell's)
  models.py      Pydantic shapes for tool arguments and results
  guard.py       Lets expected errors through the server's error masking
  app.py         Shared state for tools: settings, backend, clusters, the caller, the audit log
  server.py      Builds the FastMCP server: middleware, resources, prompts, /health
  tools/         One module per tier: read, write (submit + manage), run, admin
  cli.py         serve (default), token, keygen, check
  guide.md       The agent-facing reference, served as a resource
tests/           unit (-m unit) and integration (-m integration; starts real clusters)
```

## Commands

```sh
uv sync
uv run pytest                 # all tests, about a minute
uv run pytest -m unit         # fast, no clusters
uv run hypershell-mcp check   # what would be served
```

## Rules that hold the design together

1. **Every tool, resource and prompt carries exactly one tier tag.** Register tools only through
   `tools.tool(...)` and resources through the `resource(...)` helper in `server.py`. Authorization
   and `--disable` act on the tag; a component without one would be open to any valid token.
   `tests/test_tasks.py` and `tests/test_http.py` pin the full sets.
2. **Nothing a caller below the run tier supplies may execute or choose a file.** Submit-time
   templates go through `templates.py`, never HyperShell's engine (which runs shell and Python).
   Output paths come from the database and are confined by `outputs.inside`. `tag_tasks` changes
   tags only, and never `submitter`. `set_config` writes an allow-list. Caller patterns are
   matched by RE2 under a read deadline, and refused where a connection lacks either. If you add
   a parameter that reaches a subprocess, a path, SQL text, a JSON path or a regex, validate it
   and add a test to `tests/test_security.py` that tries to abuse it. Deny-lists lose: the first
   version of the template check was bypassed by a space.
3. **All HyperShell access goes through `Backend.call`.** One thread, one session, rollback on
   error, `Session.remove()` after. Never touch `hypershell.data` from a tool directly, and never
   import `hypershell` at module top level outside `backend.lib()`: HyperShell reads its
   configuration at import, and `cli.py` must apply `--site` first.
4. **Never write to stdout.** Over stdio it is the protocol. Anything that prints (the `hs`
   command line) runs in a child process via `App.hs` or `ClusterManager`.
5. **Children never inherit `HYPERSHELL_MCP_*`.** That prefix holds this server's secrets, and a
   cluster's environment becomes every task's environment.
6. **Expected failures raise `BackendError` or `ClusterError`** with a message fit to show a remote
   caller. `guard` passes those through; everything else is masked and logged.
7. **Schemas are read by a model on every connection.** `TaskSelector` is repeated in nine tools:
   keep its field descriptions short and put the long form in `guide.md`.
8. **Tests never touch a real HyperShell site.** `tests/conftest.py` points HyperShell at a temp
   site before importing it; child-process tests build their own environment. Keep it that way,
   and never run `hs config set --user` from a test.

## Conventions

- Python 3.11+, type hints throughout, 120-column lines, single quotes.
- Docstrings say why, and what the caller can rely on. A tool's docstring is its description to
  the model: write it for a reader deciding whether and how to call the tool.
- Commit messages: imperative summary line; no trailers.
