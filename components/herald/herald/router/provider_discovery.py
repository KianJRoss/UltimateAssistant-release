"""Provider definitions available to this Router, without adopting accounts."""
from __future__ import annotations

import shutil


def discover_login_providers() -> list[dict]:
    from herald.router import adapters, cli_auth
    from herald.router.bootstrap import _path_with_cli_locations
    from clink.registry import ClinkRegistry
    supported = cli_auth.auth_capabilities()["clis"]
    clients = ClinkRegistry()
    providers = []
    for name in clients.list_clients():
        client = clients.get_client(name)
        family = client.runner
        if family not in supported or not supported[family].get("login"):
            continue
        providers.append({"id": name, "label": name, "adapter": family,
                          "executable": client.executable[0],
                          "installed": bool(shutil.which(client.executable[0], path=_path_with_cli_locations())),
                          "login": supported[family]})
    return providers
