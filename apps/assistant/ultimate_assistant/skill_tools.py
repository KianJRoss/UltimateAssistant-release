"""Skill tools for every agent: turn a finished task into a reusable script with parameters.

Runs on the main node, next to the skill store (skills.py). See ARCHITECTURE.md (Skills).
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from .skills import SkillStore

mcp = FastMCP("ultimate-assistant-skills")


@mcp.tool()
def save_skill(name: str, description: str, script: str, runner: str = "powershell",
               parameters: list[dict] | None = None, examples: list[str] | None = None,
               test_parameters: dict | None = None) -> str:
    """Save a task you just did as a skill, so next time it runs instantly without an agent.

    name: short id (lowercase-dashes, e.g. "set-brightness"). description: what it does, in the user's terms.
    script: PowerShell (runner "powershell": declare a param() block whose names match the parameters;
    boolean parameters must be [switch]) or
    Python (runner "python": read json.loads(os.environ["SKILL_PARAMS"])). Print a short human result to
    stdout; exit non-zero on failure. parameters: the changeable parts, e.g. [{"name": "Level", "type":
    "integer", "description": "brightness 0-100", "default": 50}] -- types: string, integer, number,
    boolean, choice (with "choices"), path. Make anything that could differ next time a parameter instead
    of hard-coding it. examples: how the user asked (their exact words first) and a few other natural
    phrasings. test_parameters: values to run it with right now to prove it works (use harmless ones;
    leave out only if running it would repeat something the user wouldn't want twice). Saving again with
    the same name replaces the skill (new version)."""
    store = SkillStore()
    try:
        definition = store.save(name=name, description=description, script=script, runner=runner,
                                parameters=parameters or [], examples=examples or [])
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
