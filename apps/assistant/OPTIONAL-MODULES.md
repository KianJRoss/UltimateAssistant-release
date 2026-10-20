# Optional modules

This development feature adds install-on-request modules to the assistant.
It is separate from the v0.2.2 diagnostics release. The packaged catalog includes
planning, work, finance, manager work, Google Calendar and Actual Budget.
Browsing the catalog does not start services or install packages. Docker is not
required. Python MCP modules join the existing Herald scope; Node MCP modules
and upstream native CLIs retain their own interfaces.

## Catalog

The app reads the published catalog from the release repo (cached for a day; the
reviewed copy built into the app when offline). Each module downloads on its own,
only when installed. An owner can replace the catalog with `module-catalog.json`
beside the user's `settings.json`, and add private modules with
`module-catalog.private.json` (archives in `module-packages-private/`). This file is trusted installation metadata: do not populate
it from chat messages, web pages, or an unreviewed third-party listing. A checksum
verifies archive integrity; it does not independently establish publisher identity.
Modules execute as the user with native execution privileges. A separate Python
environment isolates dependencies, not authority or filesystem access.

Example structure (placeholder values; not an installable module):

```json
{
  "schema_version": 1,
  "modules": [{
    "id": "weekly-planning",
    "version": "1.0.0",
    "description": "Combine school, shifts, and personal commitments",
    "url": "https://example.org/weekly-planning-1.0.0.zip",
    "sha256": "REPLACE_WITH_ARCHIVE_SHA256",
    "entrypoint": "weekly_planning.server",
    "healthcheck": "weekly_planning.healthcheck",
    "setup": ["Ask which calendar, if any, the user wants to connect"],
    "accounts": [],
    "cost": "No charge"
  }]
}
```

Archive contents are Python packages at the archive root plus an optional
`requirements.txt`. Dependencies must be pinned with hashes compatible with
`pip install --require-hashes`. Each version has its own virtual environment.
Archive checks reject traversal, symlinks, checksum mismatches, and excessive size.

## Module contract

- `entrypoint` runs a stdio MCP server under `python -m`.
- `healthcheck` is a harmless read-only readiness check, exits zero, and writes
  exactly one JSON object to stdout: `{"status":"ready"}` or
  `{"status":"needs_setup"}`. It must verify the actual required capabilities,
  not just the presence of saved configuration. Failure exits nonzero.
- `ULTIMATE_ASSISTANT_MODULE_DATA` identifies the module's persistent folder.
  Keep its records and account configuration there, never in its release folder.
- The native assistant uses catalog setup guidance to configure the module and
  guides users through sign-in/consent/MFA. Never put credentials in tool replies.
- Healthcheck readiness is the module's assertion. The assistant must still make
  a harmless live tool call before claiming the requested workflow works.

## Lifecycle

`discover_optional_modules` searches the catalog and returns installation status.
`manage_optional_module` provides install, update, enable, disable, remove,
rollback, and status. Install downloads and verifies the archive, installs isolated
dependencies, and checks readiness before enabling it in the current Herald
project/part. Newly bound tools may require a fresh native assistant turn.
Restart restores previously enabled modules. A missing login keeps the module
disabled. Remove deletes software and preserves user data. Rollback switches
software to a retained previous version; modules must maintain compatible data
or implement their own recoverable migrations.

Operations serialize through an operating system lock on
`data/modules/operation.lock`. The lock is released when its process exits;
the file can remain without blocking subsequent operations. Installation errors leave the previously selected version available;
registration failures are raised rather than reported as successful activation.

Manager record and schedule checks have passed isolated synthetic checks.
The complete module lifecycle and external integrations still need verification. Before shipping it, exercise a real
small module through installation, missing setup, live discovery, updates,
failure recovery, restart, disabling, rollback, and removal.
