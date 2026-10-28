"""Notice when the user needs a module that isn't installed, and set it up without being asked.

The assistant is meant to be ahead of the user: mentioning a class, a syllabus or homework is
enough to install the school module; it shouldn't wait for "install the school module". A catalog
entry opts in with `needed_when` (when the module is needed, in plain words) and optional
`needed_examples` (messages that mean it). Each user message is checked in the background:

1. the message is compared with each candidate's description (embedding model, else word overlap);
2. a close match is confirmed by a small model ("does this message show the user needs it now?");
3. a confirmed module is installed and enabled, and the assistant says so in the conversation.

A module runs on the main node unless its catalog entry says `runs_on: "device"` (screen vision,
Windows control...), optionally limited to `platforms` (sys.platform values); a device module is
installed on each connected device that matches, through Herald's node relay. Only devices where
the module was never installed count: one the user disabled or removed is never brought back. Installing a module connects no account and spends nothing; a
module that needs an account or setup is installed and then asks for it in the conversation.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable
from urllib.parse import quote

import httpx

from . import skills
from .module_manager import ModuleManager

LOG = logging.getLogger(__name__)

NEED_SCORE = 0.6           # best cosine to a module's need or example (embedding model); a model confirms
LEXICAL_NEED_SCORE = 0.3   # word-overlap fallback without an embedding model
_VECTORS: dict[str, list[float] | None] = {}
_LOCK = threading.Lock()


def need_texts(entry: dict[str, Any]) -> list[str]:
    """The description and each example, scored separately: a short message matches one example
    better than all of them blended together."""
    return [str(text) for text in (entry.get("needed_when"), *entry.get("needed_examples", [])) if text]


@dataclass
class Target:
    """A device a module can be installed on."""
    node: str
    platform: str
    main: bool
    installed_before: set[str]
    install: Callable[[str], dict] = field(repr=False)
    capabilities: set[str] = field(default_factory=set)


def local_target(manager: ModuleManager) -> Target:
    from .module_tools import _locked
    from .node_link import is_main, node_name

    def install(module_id: str) -> dict:
        with _locked(manager):
            return manager.install(module_id)
    inventory = manager.summary()
    before = {item["id"] for item in inventory if item["installed_before"]}
    capabilities = {c for item in inventory for c in item.get("capabilities", [])}
    return Target(node_name(), sys.platform, is_main(), before, install, capabilities)


def remote_targets(router: str, headers: dict[str, str], skip: str) -> list[Target]:
    """The other connected devices, as reported by their own app through Herald's node relay."""
    try:
        nodes = httpx.get(f"{router}/nodes", headers=headers, timeout=10).json().get("nodes", [])
    except (httpx.HTTPError, ValueError):
        return []
    out = []
    for node in nodes:
        name = str(node.get("node") or "")
        if not name or name == skip:
            continue
        base = f"{router}/nodes/{quote(name, safe='')}/http/api/modules"
        try:
            here = httpx.get(base, headers=headers, timeout=10).json()
        except (httpx.HTTPError, ValueError):
            continue  # offline, or no assistant there

        def install(module_id: str, base: str = base) -> dict:
            response = httpx.post(f"{base}/{quote(module_id, safe='')}/install", headers=headers, timeout=900)
            if response.status_code >= 400:
                raise RuntimeError(response.json().get("detail") or response.text)
            return response.json()
        before = {item["id"] for item in here.get("modules", []) if item.get("installed_before")}
        capabilities = {c for item in here.get("modules", []) for c in item.get("capabilities", [])}
        out.append(Target(name, str(here.get("platform", "")), bool(node.get("main")), before, install, capabilities))
    return out


def eligible(entry: dict[str, Any], targets: list[Target]) -> list[Target]:
    """Where this module would go: the main node, or each matching device; never where it was before."""
    if entry.get("runs_on", "main") == "device":
        platforms = set(entry.get("platforms") or [])
        chosen = [t for t in targets if not platforms or t.platform in platforms]
    else:
        chosen = [t for t in targets if t.main][:1]
    provided = set(entry.get("provides_capabilities") or [])
    return [t for t in chosen if entry["id"] not in t.installed_before
            and not (provided and provided.issubset(t.capabilities))]


def candidates(manager: ModuleManager, targets: list[Target]) -> list[dict[str, Any]]:
    """Catalog modules that say when they're needed and have somewhere to be installed."""
    return [entry for entry in manager.catalog() if entry.get("needed_when") and eligible(entry, targets)]


def _vector(text: str) -> list[float] | None:
    with _LOCK:
        if text not in _VECTORS:
            _VECTORS[text] = skills.embed(text)
        return _VECTORS[text]


def score(message: str, entry: dict[str, Any], query: list[float] | None) -> float:
    """How close the message is to the module's need: its best match among description and examples."""
    texts = need_texts(entry)
    if query:
        scores = [skills.cosine(query, vector) for vector in map(_vector, texts) if vector]
        if scores:
            return max(scores)
    return max((skills.lexical(message, text) for text in texts), default=0.0)


def confirm_prompt(message: str, entry: dict[str, Any]) -> str:
    return (
        "A user said this to their assistant:\n"
        f"<<<{message[:1500]}>>>\n\n"
        f"Optional module: {entry.get('name') or entry['id']} - {entry.get('description', '')}\n"
        f"It is needed when: {entry['needed_when']}\n\n"
        "Does the message show the user needs this module now (not just a passing word)? "
        'Answer only with JSON: {"needed": true} or {"needed": false}.'
    )


def says_yes(answer: str) -> bool:
    """The confirmation, whether the model answered JSON (any key) or a bare yes/no."""
    try:
        value = json.loads(answer)
    except ValueError:
        return answer.strip().casefold().startswith(("yes", "true"))
    values = list(value.values()) if isinstance(value, dict) else [value]
    for item in values:
        if isinstance(item, bool):
            return item
        if isinstance(item, str) and item.strip().casefold() in {"yes", "true", "no", "false"}:
            return item.strip().casefold() in {"yes", "true"}
    return False


def needed(message: str, manager: ModuleManager, ask: Callable[[str], str],
           targets: list[Target], judge: Callable[[str, str], float | None] | None = None) -> list[dict[str, Any]]:
    """The not-yet-installed modules this message shows the user needs."""
    from . import decision_interface as di
    pool = candidates(manager, targets)
    if not pool:
        return []
    query = skills.embed(message)
    cutoff = NEED_SCORE if query else LEXICAL_NEED_SCORE
    out = []
    for entry in sorted(pool, key=lambda e: score(message, e, query), reverse=True):
        if score(message, entry, query) < cutoff:
            break
        # One typed yes/no: the decision model on its route when confident, else the small model confirms.
        outcome = di.yes_no(
            "module_need", f"The user needs this now, not just a passing word: {entry['needed_when']}.", message,
            llm=lambda entry=entry: says_yes(ask(confirm_prompt(message, entry))), backend=di.judge_backend(judge))
        if not outcome.ok:  # no model to confirm with: leave it for an explicit request
            LOG.info("Could not confirm whether module %s is needed", entry["id"])
        elif outcome.value:
            out.append(entry)
    return out


def note(entry: dict[str, Any], result: dict[str, Any], message: str, device: str = "") -> str:
    name = (entry.get("name") or entry["id"]) + (f" module on {device}" if device else " module")
    why = message.strip().replace("\n", " ")
    why = why if len(why) <= 80 else why[:77] + "..."
    if result.get("status") == "ready":
        return f"I set up the {name} because you said \"{why}\". It's ready to use."
    steps = "; ".join(str(step) for step in result.get("setup", [])[:3]) or "a little setup"
    return f"I installed the {name} because you said \"{why}\". To finish it needs: {steps}. Want to do that now?"


def record(manager: ModuleManager, entry: dict[str, Any], message: str, result: dict[str, Any],
           node: str = "") -> None:
    log = manager.root / "installed-when-needed.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": datetime.now(UTC).isoformat(), "module": entry["id"], "node": node,
                                 "message": message[:300], "status": result.get("status")}) + "\n")


def observe(message: str, ask: Callable[[str], str], tell: Callable[[str], None],
            manager: ModuleManager | None = None, targets: list[Target] | None = None,
            judge: Callable[[str, str], float | None] | None = None) -> list[str]:
    """Install whatever this message shows the user needs; returns "module@device" for each one set up."""
    manager = manager or ModuleManager()
    targets = targets if targets is not None else [local_target(manager)]
    done = []
    for entry in needed(message, manager, ask, targets, judge):
        for target in eligible(entry, targets):
            device = target.node if entry.get("runs_on") == "device" else ""
            try:
                result = target.install(entry["id"])
            except Exception as exc:  # noqa: BLE001 - tell the user it didn't work rather than nothing
                LOG.warning("Could not set up module %s on %s: %s", entry["id"], target.node, exc)
                where = f" on {device}" if device else ""
                tell(f"You seem to need the {entry.get('name') or entry['id']} module{where}, "
                     f"but setting it up failed: {exc}")
                continue
            target.installed_before.add(entry["id"])
            record(manager, entry, message, result, target.node)
            tell(note(entry, result, message, device))
            done.append(f"{entry['id']}@{target.node}")
    return done


def observe_in_background(message: str, ask: Callable[[str], str], tell: Callable[[str], None],
                          router: str = "", headers: dict[str, str] | None = None) -> None:
    def run():
        try:
            manager = ModuleManager()
            local = local_target(manager)
            targets = [local] + (remote_targets(router, headers or {}, local.node) if router else [])
            observe(message, ask, tell, manager, targets)
        except Exception:  # noqa: BLE001 - never break a conversation over this
            LOG.exception("Module need check failed")
    threading.Thread(target=run, name="module-needs", daemon=True).start()
