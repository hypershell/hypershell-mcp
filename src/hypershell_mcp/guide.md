# Working with HyperShell through this server

HyperShell runs large numbers of independent shell commands ("tasks") across however many
workers you give it. A database holds the queue. A **cluster** is a scheduler plus clients: the
scheduler hands tasks from the database to clients, clients run them and report back. Everything
you do here is one of: put tasks in the queue, get them run, see what happened.

## The loop

1. `status` — see the site: counts by state, what is left, what is running, what you may do.
2. `submit_tasks` — add tasks. Keep the `batch` id it returns.
3. Get them run: `start_cluster` (or `run_tasks`, which submits, runs and waits in one call).
4. `wait_for_tasks` with `{"batch": id}` — block until they finish.
5. `summarize_tasks`, `diagnose_failures`, `get_task_output` — read the outcome.
6. Fix the cause, then `retry_tasks`.

For a quick fan-out answered in one call, use `run_tasks`. For a campaign of thousands of tasks or
hours of work, use `submit_tasks` and `start_cluster`, then come back with `wait_for_tasks`:
clusters run detached and outlive the session.

## Selecting tasks

Every tool that acts on tasks takes the same `select` object. Fields are ANDed.

| Field | Meaning |
|---|---|
| `batch` | The id returned by `submit_tasks` / `run_tasks` |
| `ids` | Exact task ids |
| `status` | Any of `pending` `running` `succeeded` `failed` `cancelled` `completed` `remaining` |
| `tags` | `{"key": value}` to match a value, `{"key": null}` to match presence |
| `group`, `zone` | Task group number; zone name (`default` = no zone) |
| `where` | Conditions `FIELD OP VALUE` with `== != > >= < <= ~ !~`, e.g. `exit_status == 137`, `duration > 600`, `args ~ \.bam$`, `client_host == node042` |
| `superseded` | `false` to count each task once by its latest attempt |

## Tags

Tags are free-form `key: value` labels and the main way to slice a campaign afterwards. Tag
anything you will want to group by, such as the parameter a task varies:
`{"command": "fit --alpha 0.1", "tags": {"alpha": 0.1}}`. Then
`summarize_tasks(group_by=["tag:alpha", "status"])` shows which values fail. Keys and string
values use letters, digits and `_ . + -`.

## Templates

`submit_tasks(commands=["a.fastq", "b.fastq"], template="align --in {} --out {/-}.bam")` builds
one command per input.

| Pattern | Expands to | For `data/run1/a.fastq` |
|---|---|---|
| `{}` | the whole input | `data/run1/a.fastq` |
| `{/}` | basename | `a.fastq` |
| `{/-}` | basename without extension | `a` |
| `{-}` | path without extension | `data/run1/a` |
| `{.}` | directory | `data/run1` |
| `{+}` | extension | `.fastq` |
| `{[0]}`, `{[1:3]}` | whitespace-separated words of the input | |

## Groups and zones

**Groups** order work. Lower groups run first, and a group must finish before the next starts:
put the stages of a pipeline in groups 0, 1, 2. **Zones** place work. A task submitted with
`zone="gpu"` runs only on clients started for that zone; clients without a zone run only tasks
without one.

## Resources and limits

`cores`, `memory` (`"4GB"`) and `timeout` (seconds) can be declared per submission or per task. A
client admits a task only when it fits in what the client has left, so declaring memory is how
you stop a node from being over-committed.

## Reading failures

Exit statuses: `0` succeeded; positive is the command's own status (`127` command not found,
`137` killed, often out of memory); negative is a signal (`-9` SIGKILL, `-15` SIGTERM, `-2`
interrupted, which is how a wall-time limit ends a task); `-1` cancelled; `-1001` the client's
template could not be expanded; `-1002` the task asked for more than the client has.

`diagnose_failures` groups failures by exit status and the masked last line of stderr. Read its
clusters before reading any single task.

## Things to know

- A cluster works through the **whole** queue, in group order, not just your batch.
- One cluster runs per site at a time. `mode="forever"` keeps one serving new submissions.
- `cancel_tasks` stops pending tasks from starting. It does not interrupt a task already running;
  `stop_cluster(how="interrupt")` does.
- `retry_tasks` re-queues finished tasks and forgets their old result and output.
- Clusters started here always capture task output. Tasks run by a cluster started elsewhere
  without `--capture` have no output to read.
- Commands are stored exactly as written. `#` is not a comment marker here.
- What you may do depends on your token: `status` lists your tiers (read, submit, manage, run,
  admin). A tool you cannot use does not appear.
