# Friend-test release source and verification

Release tag: [v0.2.1](https://github.com/KianJRoss/UltimateAssistant-release/releases/tag/v0.2.1)

Selected source commit: `31b8fb217987b2484aab8aade4fe23fad2cc2b2d`. The tag points to that commit. This repository contains the selected app, bundled Herald Python source, perception MCP, Windows control MCP, and publication checks. Operational state, credentials, development probes, and maintainer-only AdminLoop modules are excluded. Speech model binaries are included in the Windows release ZIP, with licenses, and fetched with checksum verification for source installs when absent.

Windows ZIP SHA-256: `4e0872c1e082612cb9355803182c499baa1ec93ba8179cde671d9775613c123e`.

## Verification evidence

- Development Herald regression: 452 passed, 2 skipped.
- App setup/update regressions: 21 passed.
- Clean isolated installation of the exact Windows ZIP completed with Python 3.13; installed source and model bytes matched the archive.
- Fresh packaged Router/UI imports passed.
- Selected public source and installer ZIP passed filename/content publication checks.
- Installed Python dependency audit found no known vulnerabilities across 146 distributions. The editable custom Herald source was checked separately rather than treated as an upstream registry package.
- GitHub publication checks passed for the selected source commit.
- An anonymous HTTPS download matched all 114,904,128 ZIP bytes and its checksum.

## Scope

This is a Windows friend-test prerelease. The published package is ready to send for feedback; provider-specific sign-in, permissions, upstream availability, and real-world MCP session behavior remain part of that practical testing. See [release notes](RELEASE-NOTES.md) for observed limitations. This is not a new Herald registry release, a fleet deployment, or a declaration that every integration is complete.

The regular updater manifest remains at v0.2.0. The separate `preview-release.json` manifest points to this friend-test release.
