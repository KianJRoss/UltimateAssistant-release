# Source and verification

Version: 0.4.6. The implementation source commit and the download checksums are recorded in `release.json` and
`SHA256SUMS.txt`.

`apps/` and `components/` contain the Windows release bundle source, with Git text line-ending normalization. Use the attached archives for exact installation bytes. The Linux archive contains the same files,
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

The phone app's corresponding source is attached to its separate `phone-v<version>` release
as an Android source archive (Android 8.0+). Its APK is listed as `phone_app` in `release.json`.
The Docker install archive belongs to the same UA release as Windows and Linux; it includes
the Docker build context and hash-pinned Herald wheels.

Optional daily diagnostic sharing stays off on every fresh installation; consent lives only in user data,
is bound to the reviewed destination, and can be revoked.

Herald 0.4.3 includes the license and attribution for its pal-mcp-server-derived code.

License: GNU Affero General Public License v3.0 or later, see `LICENSE`.

The selected UA product revision is `9d2844fc6cdb8f2f78f270c2e3f1b54f10f9751c`.
Private development CI revision `3c82ee6217c7b405d67e686af9ac9cc1b74777dd`
contains identical packaged application files and adds platform/build verification.
Linux and Windows tests, renderer rebuilds and Node dependency audits pass.
The Android source is selected separately at `a188d08`; its release starts a new
signing identity, as recorded in `release.json`. Public downloads do not set a
hosted diagnostic collection destination; local export remains available.
