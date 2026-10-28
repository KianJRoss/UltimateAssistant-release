"""Skill tools for every agent: turn a finished task into a reusable script with parameters.

Runs on the main node, next to the skill store (skills.py) and the teaching recordings
(teach_store.py): a task the user showed once becomes a skill too. See ARCHITECTURE.md (Skills).
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP, Image

from . import teach_store
from .skills import SkillStore

mcp = FastMCP("ultimate-assistant-skills")


@mcp.tool()
def choose_known_option(request: str, options: dict[str, str]) -> str:
    """Pick among an existing inventory: themes, folders, windows, apps, or other known options.

    Supply the user's request and {exact option name: description} from a real inventory.
    Returns one of those exact names or null when none fits. Uses the user's selected
    decision route with LLM fallback. This selects an option; it does not apply changes.
    """
    from .option_decisions import choose_known_option as choose
    try:
        return json.dumps(choose(request, options), ensure_ascii=False)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


@mcp.tool()
def save_skill(name: str, description: str, script: str, runner: str = "powershell",
               parameters: list[dict] | None = None, examples: list[str] | None = None,
               test_parameters: dict | None = None, device: str = "") -> str:
    """Save a task you just did as a skill, so next time it runs instantly without an agent.

    name: short id (lowercase-dashes, e.g. "set-brightness"). description: what it does, in the user's terms.
    script: PowerShell (runner "powershell": declare a param() block whose names match the parameters;
    boolean parameters must be [switch]), bash (runner "bash", for Linux and the main node when it runs
    Linux: each parameter is an environment variable of the same name, booleans "1" or empty) or
    Python (runner "python": read json.loads(os.environ["SKILL_PARAMS"])). Choose the runner the device
    that will run it has. Print a short human result to
    stdout; exit non-zero on failure. parameters: the changeable parts, e.g. [{"name": "Level", "type":
    "integer", "description": "brightness 0-100", "default": 50}] -- types: string, integer, number,
    boolean, choice (with "choices"), path. Make anything that could differ next time a parameter instead
    of hard-coding it. examples: how the user asked (their exact words first) and a few other natural
    phrasings. test_parameters: values to run it with right now to prove it works (use harmless ones;
    leave out only if running it would repeat something the user wouldn't want twice). Saving again with
    the same name replaces the skill (new version). device: the device (node name, e.g. "kianarch") whose
    apps, screen or files the script works on; it then runs there through Herald (write it for that
    device's OS). Leave empty for skills that run on the main node."""
    store = SkillStore()
    try:
        definition = store.save(name=name, description=description, script=script, runner=runner,
                                parameters=parameters or [], examples=examples or [], device=device.strip().lower())
    except ValueError as exc:
        return f"Error: {exc}"
    message = f"Saved skill '{name}' v{definition['version']} ({len(definition['parameters'])} parameters)."
    if test_parameters is not None:
        result = store.run(name, test_parameters)
        status = "test run ok" if result["ok"] else f"TEST RUN FAILED (exit {result['exit_code']})"
        message += f" {status} in {result['seconds']}s.\nOutput: {result['output'][:1500]}"
        if not result["ok"]:
            message += f"\nError: {result['error'][:1500]}\nFix the script and save it again."
    return message


@mcp.tool()
def list_skills() -> str:
    """The learned skills: name, what each does, its parameters, and how often it ran."""
    rows = [{"name": s["name"], "description": s["description"],
             "parameters": [p["name"] + ("" if p["required"] else "?") for p in s["parameters"]],
             "runs": s.get("stats", {}).get("runs", 0), "failures": s.get("stats", {}).get("failures", 0)}
            for s in SkillStore().all()]
    return json.dumps(rows, ensure_ascii=False) if rows else "No skills learned yet."


@mcp.tool()
def show_skill(name: str) -> str:
    """One skill's full definition and script (to reuse, repair or extend it)."""
    store = SkillStore()
    definition = store.get(name)
    if not definition:
        return f"No skill named {name!r}."
    from .skills import RUNNERS
    script = (store.path(name) / RUNNERS[definition["runner"]]).read_text(encoding="utf-8")
    return json.dumps({**definition, "script": script}, ensure_ascii=False, indent=1)


@mcp.tool()
def run_skill(name: str, parameters: dict | None = None) -> str:
    """Run a saved skill with these parameter values; returns its output (or the error to fix)."""
    try:
        result = SkillStore().run(name, parameters or {})
    except ValueError as exc:
        return f"Error: {exc}"
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def delete_skill(name: str) -> str:
    """Remove a skill (when the user asks, or it's replaced by a better one)."""
    try:
        return f"Deleted '{name}'." if SkillStore().delete(name) else f"No skill named {name!r}."
    except ValueError as exc:
        return f"Error: {exc}"


@mcp.tool()
def teach_recordings(device: str = "") -> str:
    """Tasks the user showed you (teach mode): id, device, steps, screenshots, what they said it is,
    and whether you've looked at it yet. Newest first."""
    rows = teach_store.recordings(device=device or None)
    return json.dumps(rows, ensure_ascii=False) if rows else "The user hasn't shown you any tasks yet."


@mcp.tool(structured_output=False)
def learn_from_recording(id: str):
    """Learn a task the user showed you once: returns what they did, step by step (windows, controls by
    name, what they typed, passwords hidden), and the screenshot of each step.

    Then generalize it; don't replay it. Work out what the task is for and which parts would differ
    next time (a file name, a recipient, a date, an amount): those become parameters; ask the user when
    it isn't clear. Prefer the most direct way to do it: an app's own command or CLI, a file or API,
    then UI controls by their accessible name; screen coordinates last. Save it with save_skill
    (runner for the device it runs on, test_parameters with harmless values), say what you saved and
    its parameters, and offer to run it once with them watching. A task in the user's desktop apps
    runs on that device: save it with device=<the recording's device> and a script for that device's
    OS (it is sent there to run), driving apps by command, CLI or control name rather than coordinates."""
    record = teach_store.get(id)
    if record is None:
        return f"No recording named {id!r}; teach_recordings lists them."
    out: list = [teach_store.steps_text(record)]
    for index in range(len(record["images"])):
        found = teach_store.image(id, index)
        if found:
            out.append(Image(data=found[0], format="png" if found[1] == "image/png" else "jpeg"))
    return out


if __name__ == "__main__":
    mcp.run(transport="stdio")
