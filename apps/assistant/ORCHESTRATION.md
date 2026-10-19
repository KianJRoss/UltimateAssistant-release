# Scoped assistant delegation

This development integration exposes Herald's existing stateful memory, durable
run records, parallel execution and iterative loops through the assistant's MCP
gateway. It is separate from the published v0.2.2 update.

## Assistant tools

- `herald_start_assistant_work`: one native worker, a swarm of 1-4 independent
  workers, or a loop of 1-6 iterations. Requires the configured native backend,
  task, and a unique request ID. Retrying the same request returns the saved job;
  a changed task must use a new ID.
- `herald_inspect_assistant_work`: saved results, progress, failures and lineage.
- `herald_list_assistant_work`: reconnect to previous jobs within the gateway's
  project/part. Supply the current activity ID to isolate one user request.
- `herald_cancel_assistant_work`: request cancellation of a job and descendants.
  Inspect afterward. Completed edits are not rolled back.

Each CLI worker runs through its full native provider harness and current profile,
including its native tools and supported permission flags. Herald adds delegation
and scoped tools. Workers inherit explicit relevant context; they do not receive
an indiscriminate copy of all user memory or another project's context.

The Router enforces maximum depth 2, 12 jobs per tree and 8 active jobs across
this service. Native provider timeouts still apply. Loops have no automatic
side-effect retries; exhaustion without the completion marker is a failure, with
useful partial output retained. These finite loops do not create recurring
schedules. Scheduled behavior uses the existing assistant schedule tools.

## Persistence and UI

State lives under HERALD_DATA_DIR/assistant-work. Prompts, worker memory, events
and results use Herald's encrypted vaults. SQLite keeps metadata and parent IDs.
Restart marks unfinished jobs interrupted and does not replay their actions.

Chat requests persist their activity ID. The UI displays a nested delegation tree
under that request, including saved outputs/errors and cancellation controls.
Reloading reconstructs the tree. App restart/quit controls wait for active workers
or cancellation to finish.

## Memory

Relevant memory context includes dates and source-message provenance. The parent
reconciles child findings before saving durable facts. Explicit corrections can
supersede an old fact while preserving its record; superseded memories are omitted
from active retrieval. Lexical retrieval matches complete words rather than
accidental substrings. Existing SQLite data is migrated additively.

The scope restricts these gateway tools, not a user's full-permission native shell.
It is not an operating-system sandbox. No deployment or publication is performed
by this change.

## Verification

Herald full regression: 472 passed, 2 skipped. App regression: 32 passed.
Browser checks verified nested rendering, reload persistence, cancellation and
no JavaScript errors. A real Antigravity worker read an isolated file through
MCP tool invocation and the scoped Router endpoint, preserving its result and
native progress. A silent native process stopped promptly on cancellation.
Python/JavaScript syntax and Herald version-surface checks passed.

Scoped native workers receive bounded independent execution slots so a parent
can await a child on the same profile without blocking it behind the parent's
profile lease. Worker slots are reused rather than creating unlimited governor
entries. Shared conversation/resume flags are rejected for worker profiles;
the profile itself remains unchanged. Cancellation does not count as a provider
health failure. Nested native gateways inherit and enforce their parent ID.

This is verified development work. It has not been added to the public v0.2.2
installer or deployed to a friend's installation.
