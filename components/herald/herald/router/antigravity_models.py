"""Read model availability and independent quota groups from the native CLI."""
import json
import subprocess


def inventory(executable, env, cwd):
    def run(args):
        result = subprocess.run([*executable, *args], env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("Native Antigravity inventory failed")
        return result.stdout
    usage = json.loads(run(["-p", "/usage", "--output-format", "json", "--print-timeout", "20s"]))
    if usage.get("status") != "SUCCESS":
        raise RuntimeError("Native Antigravity usage inventory failed")
    groups = (usage.get("command", {}).get("data", {}).get("groups") or [])
    models = []
    for line in run(["models"]).splitlines():
        fields = line.split("\t")
        if len(fields) >= 2:
            models.append({"id": fields[0].strip(), "label": fields[1].strip()})
    if not models or not groups:
        raise RuntimeError("Native inventory returned no models or quota groups")
    return {"models": models, "groups": groups}


def available_fallbacks(executable, env, cwd, preferred=()):
    info = inventory(executable, env, cwd)
    groups = info["groups"]
    exhausted = set()
    healthy = set()
    for group in groups:
        family = "gemini" if "gemini" in group.get("name", "").lower() else "third-party" if "claude" in group.get("name", "").lower() else None
        buckets = group.get("buckets") or []
        fractions = [bucket.get("remaining_fraction") for bucket in buckets]
        if not family or not fractions or any(not isinstance(value, (int, float)) for value in fractions):
            continue
        (exhausted if min(fractions) <= 0 else healthy).add(family)
    if not exhausted:
        return []
    models = []
    for entry in info["models"]:
        model = entry["id"]
        family = "gemini" if model.startswith("gemini-") else "third-party" if model.startswith(("claude-", "gpt-")) else None
        if family in healthy:
            models.append(model)
    return [model for model in preferred if model in models] + [model for model in models if model not in preferred]
