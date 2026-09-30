from __future__ import annotations

from .settings import settings


def connector_catalog(*, connected: set[str] | None = None) -> list[dict[str, object]]:
    data = settings.user_data_dir / "connectors"
    google_credentials = data / "google" / "credentials.json"
    google_token = data / "google" / "token.json"
    obsidian_config = data / "obsidian" / "settings.json"
    return [
        {
            "id": "workspace-tools", "name": "Files and device tools", "category": "Built in",
            "status": "included", "setup": "Already included. Choose the assistant workspace in Settings; files and shell tools are scoped to that folder.",
            "user_steps": ["Choose the folder Herald may work in."],
        },
        {
            "id": "google-workspace", "name": "Google Workspace", "category": "Google",
            "status": "configured_unverified" if google_credentials.is_file() and google_token.is_file() else "setup_needed",
            "setup": "Uses the friend's own Google OAuth consent. No account credentials or tokens are copied from the developer machine.",
            "user_steps": ["Sign in to the intended Google account.", "Approve only the access needed for the requested Google services."],
            "auto_setup": "OAuth application registration is required. Inspect existing setup without reading credentials, consult official Google Workspace CLI/provider documentation, and use supported CLI or authenticated browser tools to prepare the project/client and connection. Handle technical configuration yourself. Keep new connector state in this assistant's per-user folder; do not adopt or overwrite unrelated developer accounts. Request only necessary sign-in/consent or account privileges. If no usable OAuth client or registration access exists, explain that actual blocker; do not claim that an unimplemented shared app login is available.",
        },
        {
            "id": "zotero", "name": "Zotero", "category": "Research",
            "status": "connected" if "ultimate-assistant-zotero" in (connected or set()) else "setup_needed",
            "setup": "Connects read-only to the user's Zotero library. The API key is verified, then stored in Herald's encrypted local vault; the MCP is registered only to this assistant's project.",
            "user_steps": ["Enter the Zotero user ID and a personal API key with read-only library access."],
            "auto_setup": "The app verifies credentials with Zotero, stores the API key in the encrypted Herald vault, and registers read-only library search, collection listing, and item lookup tools. The key is never sent to the model.",
        },
        {
            "id": "obsidian", "name": "Obsidian", "category": "Knowledge", 
            "status": "configured_unverified" if obsidian_config.is_file() else "setup_needed",
            "setup": "Uses the community Local REST API plugin in the user's own running Obsidian vault.",
            "user_steps": ["Install and enable the Obsidian Local REST API community plugin.", "Copy its local API key; keep Obsidian running while using the connector."],
            "auto_setup": "The app can configure and register the MCP after the plugin is enabled; plugin installation and key creation remain user actions.",
        },
    ]


def connector_setup_context(
    *, connected: set[str] | None = None, router_url: str = "",
    project: str = "", part: str = "", bridge_python: str = "",
) -> str:
    entries = connector_catalog(connected=connected)
    lines = [
        "External app/account connectors in this catalog are optional reference information, not the core onboarding checklist. Set up a connection only when the user asks for it. Core setup covers browser automation, vision, and Windows control MCPs alongside native CLI tools. Do not discover external accounts or begin their installation/login merely because they are listed here; their absence does not block core setup.",
        "Connector setup status (local app inventory; examples are not a whitelist). The active provider CLI's native tools can install and configure compatible MCP servers even when this catalog has no dedicated wizard. For new requests, use those CLI tools to inspect official instructions, perform safe setup, and verify the server; use Herald for optional project-scoped registration, swarms, and loops. Do not stop at listing prerequisites if the tools can complete setup.",
        "Google setup facts: this app bundles no shared Google OAuth client and currently has no built-in Google OAuth setup endpoint. Application source, old test transcripts and other developer projects are not a source of a user's OAuth registration. For a requested Google connection, consult the official Workspace CLI documentation at https://github.com/googleworkspace/cli and evaluate its supported setup/auth path, or another official provider path. Check whether required tooling is installed, prepare it in the assistant's user folders, and take setup as far as the user's available authority allows. Do not assume sign-in alone removes the OAuth app registration requirement.",
        f"New connector configuration belongs under {settings.user_data_dir / 'connectors'}. For Google Workspace CLI setup, set GOOGLE_WORKSPACE_CLI_CONFIG_DIR to that folder's google/gws child and CLOUDSDK_CONFIG to its google/gcloud child. This keeps connector discovery and login separate from developer sessions. Never export or read tokens to reuse a different account. Preserve every unrelated native CLI setting and MCP entry; add a dedicated assistant entry rather than replacing another gateway.",
        f"Herald Router endpoint: {router_url}; assistant MCP registration scope: project={project}, part={part}. These are configuration values, not credentials. Use only available authenticated Router APIs/tools; never print or disclose bearer tokens.",
        f"For a native CLI's MCP bridge on this device, the assistant Python is {bridge_python}; the available scoped server is `-m herald.mcp_gateway --url {router_url} --project {project} --part {part}`. Its tools are resolved live from Herald's project scope, including Vision when bound, plus project/part-restricted recurring agentic loop tools (list/create/disable/history). Only use this local Python command if the selected CLI subprocess runs on this same device. If a direct CLI session needs Vision or another Herald MCP and does not list the gateway, configure the gateway through that CLI's own documented MCP settings, then verify discovery on a fresh CLI turn. Do not equate scheduled loops or parallel chat delegation with AdminLoop's tool-using Git swarm; that specialized orchestrator is not exposed through this bridge.",
    ]
    for entry in entries:
        steps = "; ".join(str(step) for step in entry.get("user_steps", []))
        lines.append(f"- {entry['name']}: {entry['status']}. {entry['setup']} User steps: {steps}")
    lines.append("Do not claim an optional connector has been installed or authorized unless its live tools are present. Guide the user through remaining steps accurately; credentials belong only in local setup UI, never chat.")
    return "\n".join(lines)
