# Diagnostics update source and verification

Version: 0.2.2. The source commit and installer checksum are recorded in the release metadata.

This curated release contains persistent timestamped activity, explicit diagnostic sharing, Antigravity quota-group fallback and live inventory, sign-in startup, Restart app and Quit app controls, and persistent voice preferences. Optional module development and manager-work records are excluded from this update.

Evidence: full Herald regression passed 455 tests with 2 skipped; app checks passed 28 tests; focused native CLI checks passed 18 tests. A real Antigravity native file read returned the expected text. Browser checks confirmed activity survives completion and reload and native usage displays without JavaScript errors. A clean isolated install and reinstall preserved the selected voice; installed app/component bytes matched the ZIP and Kokoro was detected. Selected archive privacy checks passed. The support receiver accepted a synthetic HTTPS diagnostic upload and retains reports privately.

This is a friend-test update. It does not grant remote control, automatically transmit conversations, or claim all external integrations work.
