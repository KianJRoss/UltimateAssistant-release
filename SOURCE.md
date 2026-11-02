# Source and verification

Version: 0.4.4. The implementation source commit and the download checksums are recorded in `release.json` and
`SHA256SUMS.txt`.

`apps/` and `components/` match the Windows release bundle exactly. The Linux archive contains the same files,
plus `install.sh` and its own `INSTALL.txt`; its `apps/assistant/VERSION` uses Linux line endings, which is the only
other difference. Large speech model files download during setup with upstream checksum verification. Developer
accounts, runtime state, tests, captures and maintainer-only tools are excluded, except the routing
benchmark in `tools/decisions/` (see its README), which is outside the bundle and published so results can be
reproduced.

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

Validation for 0.4.4: the assistant decision, routing, onboarding and release test suites passed, live benchmarks
verified the 40-question parallel turn surmise (`jev`, OpenRouter `jev-latest`, `perplexity/pplx-decider-v1.2`,
`liquid/d1.1`) and local `tev1` split, and both bundles and the Herald 0.4.2 packages passed the publication check
for private markers.

License: GNU Affero General Public License v3.0 or later, see `LICENSE`.
