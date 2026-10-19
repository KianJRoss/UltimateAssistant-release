# Friend-test source and verification

Version: 0.2.3. The implementation source commit and installer checksum are recorded in release metadata.

This curated source matches the Windows release bundle, excluding large speech binaries (downloaded with upstream checksum verification when absent). Developer accounts, runtime state, tests, captures and maintainer-only control modules are excluded.

Optional daily diagnostic sharing is disabled on every fresh installation. Consent is stored only in user data, bound to the reviewed HTTPS destination, revocable, and never included in the installer. Report payloads omit all free-form action labels, backend names and message identifiers. One-time sends also require a destination preview and confirmation. The upload-only receiver stores reports privately and does not serve downloads; existing retention and storage limits remain enforced.

This is a friend-test prerelease. External provider sign-in and optional account integrations still require testing with the recipient's own accounts.
