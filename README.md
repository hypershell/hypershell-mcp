# HyperShell MCP

An [MCP](https://modelcontextprotocol.io) server that gives an AI agent a HyperShell site to work
with: submit tasks, run them on a cluster, watch them, find out why some failed, and try again.

[HyperShell](https://hypershell.org) runs large numbers of independent shell commands across
however many workers you give it, from a laptop to a leadership-class machine. This server puts
that behind 23 tools an agent can call, with typed results, over stdio for local use or over HTTP
with JWT authentication.

> **Status: a spike.** Built against HyperShell 2.9.0a6 and FastMCP 4.1. It works end to end and
> has a test suite that runs real clusters, but it has not been used in anger. See
> [`DESIGN.md`](DESIGN.md) for what was decided and what is still open.

## What it looks like

Ask an agent connected to this server to *"run the alignment over every FASTQ in `data/` with 8
workers and tell me which ones failed and why"*, and it will make calls like these:

```text
run_tasks(commands=["data/a.fastq", "data/b.fastq", ...],
          template="align --in {} --out {/-}.bam", workers=8, tags={"stage": "align"})
  → {batch: "20261110-221530-a3f1", done: true,
     counts: {succeeded: 46, failed: 4, ...}, results: [...]}

diagnose_failures(select={"batch": "20261110-221530-a3f1"})
  → {failed: 4, clusters: [
       {exit_status: 137, meaning: "killed (SIGKILL): often out of memory", count: 3,
        hosts: {"node07": 3}, example_stderr: "..."},
       {exit_status: 1, signature: "ValueError: truncated record at line <n>", count: 1, ...}],
     advice: ["3 of 4 failures were on node07: suspect that node, not the tasks."]}

retry_tasks(select={"batch": "20261110-221530-a3f1", "where": ["exit_status == 137"]})
```

## Install and run

Requires Python 3.11 or later. From a checkout:

```sh
uv sync
uv run hypershell-mcp check      # show the site, database, auth and tools it would serve
uv run hypershell-mcp            # serve over stdio
```

The server acts on **one HyperShell site**: whichever one its environment selects, exactly as
`hs` would in the same shell (`HYPERSHELL_SITE`, `HYPERSHELL_DATABASE_FILE`, the HyperShell
config files). `--site DIR` picks a site directory explicitly.

### Connect a local agent (stdio)

Claude Code:

```sh
claude mcp add hypershell -- uv run --directory /path/to/hypershell-mcp hypershell-mcp
```

Claude Desktop and most other clients take the same thing as JSON:

```json
{
  "mcpServers": {
    "hypershell": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/hypershell-mcp", "hypershell-mcp"],
      "env": { "HYPERSHELL_SITE": "/path/to/project" }
    }
  }
}
```

Over stdio there is no authentication: the server runs as you, for you, and offers every tool.
Add `--read-only` to offer only the tools that look.

### Serve over HTTP

HTTP needs a key. Make one, start the server, and sign a token for each caller:

```sh
hypershell-mcp keygen ~/.config/hypershell-mcp/secret
hypershell-mcp --transport http --port 8000 --jwt-secret-file ~/.config/hypershell-mcp/secret

hypershell-mcp token --secret-file ~/.config/hypershell-mcp/secret \
    --subject ada --scopes read,submit --ttl 8h
```

```sh
claude mcp add --transport http hypershell http://127.0.0.1:8000/mcp \
    --header "Authorization: Bearer <token>"
```

The server binds to `127.0.0.1` unless told otherwise, and it speaks plain HTTP: to reach it from
another machine, put it behind TLS (a reverse proxy, or an SSH tunnel to the login node), since a
bearer token is only as private as the connection it crosses. `GET /health` answers without a
token, for monitors.

## Tools

Every tool belongs to one **tier**. Over HTTP a caller gets a tier only if their token carries
its scope (`hypershell:read`, `hypershell:submit`, …); tools they cannot use are not listed for
them at all.

| Tier | Tools | What the tier can do |
|---|---|---|
| **read** | `status` `list_tasks` `summarize_tasks` `get_task` `get_task_output` `diagnose_failures` `wait_for_tasks` `list_tags` `get_config` `list_clusters` | Look. Changes nothing. |
| **submit** | `submit_tasks` | Add tasks to the queue. |
| **manage** | `cancel_tasks` `retry_tasks` `tag_tasks` `delete_tasks` | Change tasks that exist. |
| **run** | `run_tasks` `start_cluster` `stop_cluster` | Start and stop the clusters that execute tasks. |
| **admin** | `init_database` `vacuum_database` `backup_database` `rotate_database` `set_config` | Database upkeep and configuration. |

A few are worth knowing about:

- **`status`** is the place to start and to return to. One call gives counts by state, what is
  left by group and zone, throughput, running clusters, the caller's tiers, and `hints` about what
  looks worth doing next.
- **`run_tasks`** submits commands, makes sure a cluster is serving the queue, waits, and returns
  each command's exit status and output. A parallel `for` loop in one call.
- **`submit_tasks`** returns a **batch id**. Every other tool accepts `{"batch": id}` to mean
  exactly those tasks.
- **`summarize_tasks`** counts tasks by any mix of status, group, zone, exit status, host or tag
  value, with duration statistics: "which values of `alpha` fail", "which nodes are slow".
- **`diagnose_failures`** groups failed tasks by exit status and the last line of stderr with ids,
  paths and numbers masked, so ten thousand failures read as the three causes they are.
- **`wait_for_tasks`** blocks with progress reports, and returns early, saying so, when tasks are
  pending but nothing exists to run them.
- **`start_cluster`** runs a real `hs cluster`: locally, over SSH, under MPI, or through a launcher
  such as `srun`, with autoscaling. It runs detached and outlives the session that started it.

### Selecting tasks

Tools that act on tasks share one `select` argument. Its fields are ANDed:

```json
{"batch": "20261110-221530-a3f1",
 "status": ["failed"],
 "tags": {"stage": "align", "reviewed": null},
 "where": ["duration > 600", "client_host == node07", "args ~ \\.fastq$"]}
```

`status` takes `pending`, `running`, `succeeded`, `failed`, `cancelled`, `completed` or
`remaining`. A tag value of `null` means "has this tag". `where` conditions use
`== != > >= < <= ~ !~` on any task field. Tools that change tasks refuse an empty selector unless
told `everything: true`, and `delete_tasks` previews unless told `confirm: true`.

### Resources and prompts

| Resource | Content |
|---|---|
| `hypershell://guide` | The reference an agent needs: workflow, selectors, templates, tags, groups, zones, exit statuses |
| `hypershell://status` | Site status as JSON |
| `hypershell://config` | Effective configuration, secrets masked |
| `hypershell://task/{id}` and `…/{id}/stdout`, `…/stderr` | One task; its captured output |
| `hypershell://batch/{batch}` | Counts for one batch |
| `hypershell://cluster/{id}/log` | A cluster's log |
| `hypershell://help/{command}` | `hs <command> --help`, live from the installed HyperShell |

Prompts: `parameter_sweep`, `triage_failures`, `campaign_report`, `resume_campaign`.

## Security

Be clear about what this is: **a server that runs shell commands.** A task is a command line, and
a cluster executes tasks as the user the server runs as.

- **Tiers limit what a token can do by itself.** A `read` token can look and nothing else. A
  `submit` token can queue commands but not start anything that runs them. Only `run` can execute,
  and only `admin` can touch the database files or configuration. Give each caller the least that
  does the job, with a short `--ttl`.
- **But queued commands do run once anyone starts a cluster.** `submit` is therefore a deferred
  form of `run`: treat a `submit` token as able to execute, on whoever's cluster drains the queue.
- **Anything a task can do, the server's user can do**, including reading the signing secret if it
  is a file that user can read. With a shared secret (HS256), a task can therefore mint itself a
  token for any tier. To separate *verifying* from *signing*, use a key pair: `keygen --type ec`,
  give the server only the public key (`--jwt-public-key`), and sign tokens somewhere tasks do not
  run. A JWKS endpoint (`--jwks-uri`) does the same with an identity provider.
- **Switch off what a deployment does not need.** `--read-only`, or `--disable run,admin`, removes
  tiers for every caller regardless of token.
- **HTTP refuses to start without a key.** `--no-auth` exists for development and only binds
  loopback.

What the server does on its own account:

- Tokens must be signed with the configured key and algorithm, carry `exp` (and not be past it),
  match the issuer and audience, and not have a future `nbf`. Secrets shorter than 32 bytes are
  refused at startup.
- Commands are stored exactly as given. Submit-time templates are limited to text substitution;
  HyperShell's shell (`{% %}`) and Python (`{= =}`) expansions are refused, because they would run
  inside the server for a caller who may only submit.
- Task output is read only from the site's task-output directory (plus any listed in
  `mcp.outputs`), with symlinks resolved, because output paths are data in the database.
- `get_config` masks keys, passwords, secrets and tokens. `set_config` cannot change the `mcp`
  section.
- The server's own `HYPERSHELL_MCP_*` environment is stripped from every cluster it starts, so it
  does not reach tasks.
- Every call outside the read tier is appended to `<site>/mcp/audit.jsonl`: time, token subject,
  tool, arguments, outcome. Denied calls are recorded too. Tasks submitted with a token are tagged
  `submitter:<subject>`.

## Configuration

Settings come from an `[mcp]` section in the HyperShell configuration, then `HYPERSHELL_MCP_*`
environment variables, then command-line options.

```toml
[mcp]
transport = "http"            # or "stdio"
host = "127.0.0.1"
port = 8000
path = "/mcp"
disable = ["admin"]           # tiers to switch off;  readonly = true  switches off all but read
outputs = ["/scratch/hs/task"]   # extra directories task output may be read from

[mcp.jwt]
secretfile = "~/.config/hypershell-mcp/secret"   # or: secret, pubkey (PEM or path), jwks (URL)
algorithm = "HS256"           # default: HS256 with a secret, RS256 with a key
issuer = "hypershell-mcp"     # required claims; "" accepts any
audience = "hypershell-mcp"

[mcp.limit]
rows = 500                    # most tasks one listing returns
output = 32768                # most bytes of task output one call returns
submit = 10000                # most tasks one submit call accepts
wait = 600                    # longest one wait call blocks, in seconds
```

`mcp.jwt.secret` can use HyperShell's `_env` and `_eval` suffixes (`secret_env = "MY_SECRET"`).
`hypershell-mcp check` prints the result of all of this without serving.

## How it works

- **Reads and writes go through the HyperShell library in-process**, on one dedicated thread with
  one database session, rolled back on error and released after each call. That is the discipline
  HyperShell's own threads keep and what Turso requires.
- **Clusters are real `hs cluster` processes**, detached, with their record, log and exit status
  under `<site>/mcp/clusters/<id>/`, so a later server process can find and stop clusters it did
  not start. One cluster runs per site, because a HyperShell server re-queues tasks it finds
  scheduled but unfinished when it starts.
- **Database upkeep goes through the `hs` command line** in a child process, as an operator would
  run it.
- **Expected errors are shown; unexpected ones are masked.** "No task with that id" reaches the
  caller as written; a bug reaches them as "Error calling tool" and the operator's log as a
  traceback.

## Development

```sh
uv run pytest               # everything, about a minute; runs real clusters in a temp site
uv run pytest -m unit       # no clusters, a few seconds
```

Tests never touch a real HyperShell site: the session creates a throwaway one before HyperShell
is imported.

## License

Apache-2.0.
