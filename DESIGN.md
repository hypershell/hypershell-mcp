# Design notes

What was decided in building this spike, why, what was checked, and what is still open. The
README says how to use the server; this says how to think about it.

## The shape

An agent working with HyperShell wants to do four things: put work in the queue, get it run, see
what happened, and fix what went wrong. The server is built around that loop, not around
HyperShell's command line. So there is no `hs_list` wrapping `hs list`. There is `status`, which
answers "where do things stand and what should I do"; `summarize_tasks`, which the command line
has no equivalent of; and `diagnose_failures`, which does the reading a person would otherwise do
by hand across a thousand stderr files.

Three ideas carry most of the design:

- **The batch.** Every submission returns a batch id, stored as a tag. `{"batch": id}` then
  selects exactly those tasks in every other tool. An agent never has to carry a list of ids.
- **One selector.** Every tool that acts on tasks takes the same `select` object. Learn it once.
- **Tiers.** Every tool belongs to one of five tiers, and a tier is what a token scope grants, what
  `--disable` removes, and what the audit log records. There is no per-tool policy to get wrong.

## Decisions

**1. The library in-process for data, the command line as a subprocess for everything that runs.**
Queries and writes go through `hypershell.data.model` directly: fast, typed, and no parsing of
printed output. Clusters and database upkeep go through `hs`, because that is the supported
interface for them, because those code paths print to stdout (which over stdio is the protocol),
and because a cluster has process-global state that does not belong inside a server.

**2. One site per server.** HyperShell reads its configuration once, at import, into module
globals. Rather than fight that, the server is bound to the site its environment selects, as `hs`
is. Several sites means several servers. `cli.py` applies `--site` before anything imports
HyperShell, and `backend.lib()` is the only place that does import it.

**3. All database access on one thread.** HyperShell's session is thread-local and its own rule
is one thread per connection, which Turso enforces. `Backend.call` runs every operation on one
dedicated thread, rolls back on any error and releases the session afterwards, so a failed call
cannot poison the next and every read sees current data.

**4. Commands are stored verbatim.** HyperShell's submit path strips everything after a `#` and
reads inline `# HYPERSHELL:` tag comments. That silently truncates `echo 'issue #42'` (it is on
HyperShell's own roadmap as a defect), and per-task options are better as typed fields than as a
comment mini-language. So `submit_tasks` calls `Task.new(parse_inline=False)` and offers per-task
`tags`, `group`, `cores`, `memory` and `timeout` as an object.

**5. Submit-time templates are our own, text-only.** HyperShell's template engine can run a shell
command or evaluate Python per input. At submit time that would execute in the server process
for a caller who may only queue tasks. The first version refused `{%` and `{=` and passed the rest
to HyperShell's engine; the review got past it with `{ % cmd % }`, because the engine strips
padding. Deny-lists lose. `templates.py` now implements the textual patterns itself and the
engine never sees a caller's template. A cluster can still apply a full template when it runs
tasks: that is the run tier, which executes anyway.

**6. Clusters are detached processes with records on disk.** A campaign should outlive the MCP
session that started it, especially over stdio where the server lives only as long as the client.
Each cluster gets a directory (`<site>/mcp/clusters/<id>/`) with its record, log and exit status,
so a later server process can list it, read its log and stop it. Liveness for a cluster this
process did not start is checked against the process's command line, which carries the cluster's
own directory: a recycled pid is never reported or signalled as ours.

**7. One cluster per site.** A HyperShell server re-queues tasks it finds scheduled but
unfinished when it starts. A second server on the same database would take the first one's
running tasks and run them again. So `start_cluster` refuses while one runs, and also refuses
(unless forced) when clients are connected to a server it did not start.

**8. Waiting never hangs on an unserved queue.** `wait_for_tasks` returns early, saying so, when
tasks are pending and nothing exists to run them. `run_tasks` uses the same detection to start a
cluster instead. A killed HyperShell server leaves its clients recorded as connected forever, so
the server discounts clients that connected before its own last cluster ended.

**9. Destructive changes are previews first.** MCP elicitation would be the natural way to confirm
a delete, but it is not available on connections using the 2026-07-28 protocol, which is what
current clients negotiate. So `delete_tasks` is a preview unless `confirm` is true, the other
changes take `dry_run`, and every one of them refuses an empty selector unless told `everything`.

**10. Expected errors are shown, unexpected ones masked.** The server runs with FastMCP's error
masking on, so a bug never sends a path or a query to a remote caller. Messages meant for the
caller are raised as `BackendError` or `ClusterError` and passed through by `guard`.

**11. Schemas are written for the reader that pays for them.** Every connected model reads every
tool schema. The selector is repeated in nine tools, so its field descriptions are one line each
and the long form lives in the guide resource. Tool descriptions say when to use the tool and what
it will not do, not just what it is.

**12. Settings live in HyperShell's configuration.** An `[mcp]` section, `HYPERSHELL_MCP_*`
variables and command-line options, in that order, with single-word keys so the environment form
is unambiguous. An operator configures the server the way they configure `hs`.

## Security

The threat model is in the README. The essentials:

- HTTP requires a JWT verified with a key. The verifier is FastMCP's, tightened: a token must carry
  `exp`, must not have a future `nbf`, and an HS256 secret must be at least 32 bytes.
- A caller's power is their token's scopes, intersected with the tiers the server offers.
- `submit` is deferred execution and `run` is execution. Nothing below `run` may execute or choose
  a file by its own call. That sentence is the invariant the review was run against.
- A shared secret can be read by any task the server's user runs. Asymmetric keys remove that:
  the server holds a public key and tokens are signed elsewhere.

### What the adversarial reviews found

After the first build, an independent reviewer was given the code and the invariant and asked to
break it. It reported thirteen findings, all reproduced. A second reviewer was then asked to break
the fixes, and found that one of them did not hold. Every finding from both rounds is fixed and
has a regression test in `tests/test_security.py`, which is the record of what was tried.

What the server does differently because of them:

- Submit templates are expanded by `templates.py` and never reach HyperShell's engine
  (decision 5).
- `set_config` writes an allow-list of scheduling and limit settings, each with a typed value.
- Bound to loopback, the HTTP server validates `Host` and `Origin`.
- Every caller pattern is matched by RE2, and every read runs under a time budget. Where a
  database connection cannot be given both, pattern conditions are refused, not passed through.
- Redaction covers exported variables, secret-named tables, and the credentials, query and
  fragment of any URL.
- Tags are labels only: the names of typed task fields are refused, every value is checked, and
  `submitter` and `batch` are stamped by the server after the caller's tags.
- Templates, commands, tag counts and memory sizes have fixed forms and limits.
- Cluster records are written atomically and read tolerantly, starts are serialised, and
  `run_tasks` always returns its batch, whatever happens to the cluster.
- The server's own secrets are stripped from the environment of everything it starts.

Two lessons worth keeping. The most serious finding was in a check written *for exactly that
threat*, and tested: a deny-list in front of a powerful engine loses, and the fix was to stop
using the engine. And the first fix for caller patterns repeated the mistake one level up,
bounding the dangerous operation instead of replacing it. The second-round fixes have not
themselves been independently reviewed.

## What was checked, and how

- **98 tests**, about a minute. They run against a throwaway site and include real `hs cluster`
  processes: submit, run, read output, diagnose, retry, drain, interrupt, kill, back up, vacuum,
  rotate; the command line as child processes over stdio and over HTTP with tokens; and the
  security tests above.
- **Two trials with a real agent** (a headless Claude Code session given only this server): a
  parameter sweep through `run_tasks` over stdio, and a 40-task campaign with permanent and
  transient failures over HTTP with a token. Both reached the right answer.
- **On macOS, SQLite, Python 3.12 (unit tests also on 3.11 and 3.14), HyperShell 2.9.0a6,
  FastMCP 4.1.8.**

## What was not checked

- **PostgreSQL.** The backend has PostgreSQL branches (JSON tag extraction, `jsonb_object_keys`,
  `date_trunc` buckets) written from the documentation and never run. Pattern conditions (`~`)
  are refused there, and the read time budget is SQLite's: PostgreSQL would need its own.
- **Turso.** It shares SQLite's dialect, so most of it should hold, but none of it was run.
  Pattern conditions are offered only if its driver accepts the matcher and the time budget, and
  are refused otherwise.
- **Linux**, and any real cluster: the `ssh`, `mpi` and custom launchers and autoscaling are passed
  through to `hs cluster` and tested only as far as the argument list.
- **JWKS** against a real identity provider.
- **Scale.** Nothing here was run against a database of millions of tasks. `summarize_tasks` and
  `count` are single aggregate queries and should be fine; `tag_tasks` reads and rewrites each
  matched row's tags; `diagnose_failures` reads up to 2,000 files.

## Open questions and next steps

1. **Ownership.** Today `manage` can cancel or delete anyone's tasks. A shared deployment probably
   wants "your own tasks unless you hold a wider scope", using the `submitter` tag that is already
   stamped.
2. **Revocation.** A token is valid until it expires. A deny-list of `jti` values is small to add.
3. **Standard OAuth discovery.** With `--jwks-uri` the server verifies an identity provider's
   tokens, but does not publish protected-resource metadata, so a client cannot discover how to
   log in. FastMCP's `RemoteAuthProvider` adds it.
4. **Background tasks.** A wait is capped (ten minutes by default) and the caller calls again.
   MCP's task protocol would let a long wait run in the background; in FastMCP 4.1 it needs the
   separate `fastmcp-tasks` extension, which was left out.
5. **Rotated partitions.** After `rotate_database`, completed tasks live in partition files that
   the `hs` command line unions into listings and this server does not read.
6. **Tasks from a file on the server.** HyperShell's bread and butter is a task file. Reading an
   arbitrary server-side path on behalf of a submit-tier caller is a file-read primitive, so it is
   not offered. A confined directory would make it safe.
7. **Relay, the live queue, `hs client`.** Not exposed.
8. **Packaging.** Not on PyPI; the name and the `[mcp]` config section are provisional.

## Things learned about HyperShell along the way

Building against the library turned up defects in HyperShell itself. They are recorded for
HyperShell's own roadmap and are not listed here.

It also showed what the library could offer that would make a tool like this simpler:

- a way to submit and get the new ids back (`submit_from` returns a count);
- the query builder behind `hs list` as a public function;
- `python -m hypershell`;
- a zone filter by name in listings.
