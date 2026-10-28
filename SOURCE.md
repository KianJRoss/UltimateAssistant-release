# Source and verification

Version: 0.4.0. The implementation source commit and the download checksums are recorded in `release.json` and
`SHA256SUMS.txt`.

`apps/` and `components/` match the Windows release bundle exactly. The Linux archive contains the same files,
plus `install.sh` and its own `INSTALL.txt`; its `apps/assistant/VERSION` uses Linux line endings, which is the only
other difference. Large speech model files download during setup with upstream checksum verification. Developer
accounts, runtime state, tests, captures and maintainer-only tools are excluded.

- **Herald** (the Router the assistant runs on) is its own product: setup downloads `herald-router` and
  `herald-sdk` from https://github.com/KianJRoss/herald-release, pinned with SHA-256 in
  `apps/assistant/herald.json`.
- **Optional modules** are separate downloads listed in `modules/catalog.json`, each a release here
  (tag `module-<id>-v<version>`) with its SHA-256 in the catalog. The assistant installs one only when it's needed.

The installed app checks itself against `apps/assistant/core-manifest.json` (every core file's SHA-256) so a
changed core is noticed before an update replaces it; device setup fixes and the user's own agents, skills and
extensions live in the user data folder and survive updates.

The phone app's source is in `companions/android` (Android 8.0+); its signed APK is a separate release
(`phone-v<version>`), listed as `phone_app` in `release.json`.

Optional daily diagnostic sharing stays off on every fresh installation; consent lives only in user data,
is bound to the reviewed destination, and can be revoked.

Validation for 0.4.0: the full assistant test suite passed. Both bundles and the Herald packages passed the
publication check for private markers. Fresh installations were verified from these exact files on a clean
Windows 11 guest (Python 3.13 provisioned by the installer, app started at sign-in after a reboot, local Router
serving models) and on a fresh Linux (Omarchy) user account (installer, user service at login, local Router).

License: GNU Affero General Public License v3.0 or later, see `LICENSE`.
