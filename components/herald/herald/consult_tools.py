"""Herald model-consultation tool -- an MCP server that lets any agent (a
native CLI running in window mode, or Herald's own harness) call back into
Herald's router to delegate work to other models, in parallel and/or nested
depth. Mirrors the nested-consultation pattern from the PAL MCP server this
was originally inspired by: a depth counter and max-depth cap travel forward
as plain values (env vars there, an explicit `consult_depth` request field
here) since each delegated call is a separate process/request with no
shared in-memory state to rely on.

Transport: stdio (launched as a subprocess by Herald's bootstrap).
"""
from __future__ import annotations

import concurrent.futures
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("herald-consult")

DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_PARALLEL = 5


def _exhausted_accounts() -> set[str]:
    """Herald already tracks live CLI subscription quota (the same data
    `herald usage` shows) -- consult it before picking a target instead of
    finding out only after a call fails.

    This tool runs as a fresh subprocess per invocation, so it does NOT
    share the router process's in-memory usage cache -- calling
    cli_usage.all_usage(refresh=False) directly here would always see an
    empty cache (a background refresh has no time to finish before this
    short-lived process exits) and silently return nothing useful. Fetch
    the router's own warm cache over HTTP instead (GET /usage/cli), then
    reuse the same exhaustion logic (quota_router.get_exhausted_backend_names)
    against that real data. Best-effort: if the router is unreachable or the
    quota tracker errors, treat nothing as exhausted rather than blocking
    delegation on a diagnostics failure.
    """
    try:
        from herald.client import RouterClient
        from herald.router import quota_router
        usage_data = RouterClient()._get("/usage/cli").get("clis", [])
        return quota_router.get_exhausted_backend_names(usage_data)
    except Exception:
        return set()


@mcp.tool()
def consult_models(tasks: list[dict[str, Any]], parallel: bool = True) -> str:
    """Delegate one or more tasks to other Herald-routed models -- the
    "pyramid of workers" primitive: call this from within an agentic
    session to fan work out to other models (parallel) or to hand a
    sub-task to a specialist model and use its answer (sequential/depth).

    Each entry in `tasks` is {"model": "<name-or-policy>", "prompt": "<...>"}
    OR {"model": ["<primary>", "<fallback>", ...], "prompt": "<...>"}.
    `prompt` must be specific and self-contained -- the target model has no
    memory of this conversation. `model` may be a specific CLI account
    (e.g. "codex-primary") -- which then runs in its own native window mode,
    same as this session -- or a routing policy (e.g. "balanced", "quality")
    for Herald to choose automatically. When `model` is a list, it's tried
    in order: Herald's live CLI-subscription quota data is checked first to
    skip candidates already known to be exhausted, and if a call still
    fails (quota hit mid-call, backend down, etc.) the next candidate in
    the list is tried automatically -- always give a fallback list for any
    task where the specific target matters, since a single named account
    has no other safety net if it's out of quota.

    parallel=True (default): all tasks run concurrently, independent of
    each other. parallel=False: tasks run one at a time in order, and each
    task's prompt has the previous task's result appended as context --
    use this for a sequential refinement chain rather than independent
    fan-out.

    Depth is capped (default 3, override with HERALD_CONSULT_MAX_DEPTH) to
    prevent runaway recursion if a delegated model itself calls
    consult_models again -- once at max depth, delegation is refused with a
    clear error rather than silently spawning forever.
    """
    depth = int(os.environ.get("HERALD_CONSULT_DEPTH", "0"))
    max_depth = int(os.environ.get("HERALD_CONSULT_MAX_DEPTH", str(DEFAULT_MAX_DEPTH)))
    if depth >= max_depth:
        return (
            f"[error] consult_models refused: already at depth {depth}/{max_depth}. "
            "Answer directly instead of delegating further."
        )
    if not tasks:
        return "[error] consult_models requires at least one task"

    max_parallel = int(os.environ.get("HERALD_CONSULT_MAX_PARALLEL", str(DEFAULT_MAX_PARALLEL)))
    if len(tasks) > max_parallel:
        # Soft cap, matching PAL's advisory-only sibling limit -- warn in
        # the output rather than blocking the call outright.
        over_note = (
            f"[warning] {len(tasks)} tasks requested, exceeding the advisory "
            f"parallel cap of {max_parallel} -- proceeding anyway.\n\n"
        )
    else:
        over_note = ""

    from herald.client import RouterClient
    client = RouterClient()

    def _run_one(task: dict[str, Any], extra_context: str = "") -> str:
        model_spec = task.get("model")
        prompt = task.get("prompt")
        if not model_spec or not prompt:
            return "[error] each task requires 'model' and 'prompt'"
        candidates = model_spec if isinstance(model_spec, list) else [model_spec]
        if not candidates:
            return "[error] 'model' must be a non-empty string or list"

        exhausted = _exhausted_accounts()
        # Known-exhausted candidates go last instead of being dropped --
        # quota data can be stale, and a demoted candidate is still better
        # than no candidate if every option is currently marked exhausted.
        ordered = sorted(candidates, key=lambda m: m in exhausted)

        full_prompt = f"{prompt}\n\n{extra_context}" if extra_context else prompt
        attempts: list[str] = []
        for i, model in enumerate(ordered):
            ok, content = client.chat_with_status(
                full_prompt, model=model, agentic=True, consult_depth=depth + 1,
            )
            if ok:
                chain_note = f" (fallback #{i + 1}, tried: {', '.join(ordered[:i])})" if i else ""
                return f"### {model}{chain_note}\n{content}"
            attempts.append(f"{model}: {content}")
        return "### [all candidates failed]\n" + "\n".join(attempts)

    if parallel:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(tasks))) as pool:
            results = list(pool.map(_run_one, tasks))
    else:
        results = []
        context = ""
        for task in tasks:
            result = _run_one(task, extra_context=context)
            results.append(result)
            context = f"Previous delegated result:\n{result}"

    return over_note + "\n\n".join(results)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
