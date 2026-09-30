"""Register a first real set of backends. Run once (or re-run any time --
register() upserts by name). Extend this as more backends come online."""
from registry import Registry

r = Registry()

r.register(backend_type="cli", name="claude-cli", config={"cli_name": "claude"}, priority=10)
r.register(backend_type="cli", name="codex-cli", config={"cli_name": "codex"}, priority=15)
r.register(backend_type="cli", name="antigravity", config={"cli_name": "antigravity"}, priority=20)
r.register(backend_type="cli", name="antigravity-gemini", config={"cli_name": "profile_gemini"}, priority=21)
r.register(backend_type="cli", name="antigravity-claude", config={"cli_name": "profile_claude"}, priority=22)
r.register(backend_type="cli", name="antigravity-gpt", config={"cli_name": "profile_gpt"}, priority=23)
r.register(backend_type="api_key", name="gemini-2.5-flash", config={"model_name": "gemini-2.5-flash"}, priority=10)

print("seeded:", [b.name for b in r.list_all()])
