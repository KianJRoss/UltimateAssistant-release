# Source and verification

Version: 0.3.0. The implementation source commit and installer checksum are recorded in `release.json`.

This curated source matches the Windows release bundle exactly. Large speech model files download during
setup with upstream checksum verification. Developer accounts, runtime state, tests, captures and
maintainer-only tools are excluded.

- **Herald** (the Router the assistant runs on) is its own product: setup downloads `herald-router` and
  `herald-sdk` from https://github.com/KianJRoss/herald-release, pinned with SHA-256 in
  `apps/assistant/herald.json`.
- **Optional modules** are separate downloads listed in `modules/catalog.json`, each a release here
  (tag `module-<id>-v<version>`) with its SHA-256 in the catalog. The assistant installs one only when it's needed.

Optional daily diagnostic sharing stays off on every fresh installation; consent lives only in user data,
is bound to the reviewed destination, and can be revoked.

Validation: the full assistant test suite passed (320 tests). The bundle, the 13 module archives and the
Herald packages passed publication checks with private markers. An isolated Windows installation from the
bundle installed Herald from its public release and verified each package's checksum.
