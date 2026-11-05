"""Read installed g4f adapters without letting one broken lazy import hide all others."""


def discover(provider_module):
    providers, unavailable = [], []
    for name in tuple(provider_module.ProviderUtils.convert.keys()):
        try:
            provider = getattr(provider_module, name)
            providers.append({"id": name, "needs_auth": bool(getattr(provider, "needs_auth", False)),
                              "working": bool(getattr(provider, "working", True))})
        except Exception:  # optional adapters can fail importing or resolving an alias
            unavailable.append(name)
    return {"providers": providers, "unavailable": unavailable}
