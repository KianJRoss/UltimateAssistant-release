"""Keeping changes where updates won't lose them (layers.py): the core stays as released, device
fixes and the user's own builds live in their own layers, and core changes go to the developer."""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from . import layers

mcp = FastMCP("ultimate-assistant-layers")


@mcp.tool()
def assistant_layers() -> str:
    """Whether the core is as released (intact / modified / development), this device's recorded
    setup fixes, the user's own agents, skills and extensions, the environment layer (recorded
    changes to the OS and files outside the assistant; the environment tools on each device), and
    contributions written up. Check before changing anything about the assistant itself."""
    return json.dumps(layers.layers_status(), ensure_ascii=False)


@mcp.tool()
def record_device_fix(summary: str, reason: str, kind: str, recheck: str, touched: list[str] | None = None) -> str:
    """Record what you did to make this device work (installed a dependency, set a path, a driver,
    a config value, a workaround), with how to re-check it. Every install differs; recorded fixes
    survive updates and are re-checked after one. kind: config, dependency, path, driver,
    workaround, or core-edit (you had to change a core file: also write it up with
    propose_change, since the next update will undo it)."""
    return json.dumps({"fix": layers.record_device_fix(summary, reason=reason, kind=kind, recheck=recheck, touched=touched)},
                      ensure_ascii=False)


@mcp.tool()
def remove_device_fix(fix_id: str) -> str:
    """Forget a recorded device fix that is no longer needed (e.g. an update made it unnecessary)."""
    return json.dumps({"removed": layers.remove_device_fix(fix_id)})


@mcp.tool()
def propose_change(kind: str, title: str, why: str, files: list[dict] | None = None) -> str:
    """Write up a change for the assistant's developer, kept here until the user decides to send it.
    kind: core-change (the core needs fixing or improving: a diff or the changed text in files),
    extension (something built for this user that others could use), device-fix (what this kind of
    device needed). files = [{"path", "content"}]. Leave out anything personal."""
    item = layers.propose(kind, title, why, files)
    return json.dumps({"contribution": {k: item[k] for k in ("id", "kind", "title")},
                       "next": "offer to send it with preview_contribution"}, ensure_ascii=False)


@mcp.tool()
def preview_contribution(contribution_id: str) -> str:
    """Exactly what would be sent and where, plus a confirmation code. Show it to the user; send only
    after they say yes."""
    from .diagnostics import destination
    item = layers.contribution(contribution_id)
    try:
        url = destination().get("url", "")
    except ValueError:
        url = ""
    return json.dumps({"would_send": layers.payload(item), "to": url or "not configured in this build",
                       "confirmation": layers.digest(item),
                       "note": "Only this write-up and its files are sent: no conversations, settings, files or "
                               "account details. The receiving server sees the network address."}, ensure_ascii=False)


@mcp.tool()
def send_contribution(contribution_id: str, confirmation: str) -> str:
    """Send a previewed contribution to the developer, only after the user agreed to that preview."""
    return json.dumps(layers.send(contribution_id, confirmation))


if __name__ == "__main__":
    mcp.run()
