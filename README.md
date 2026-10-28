# Ultimate Assistant

[![Latest release](https://img.shields.io/github/v/release/KianJRoss/UltimateAssistant-release?label=release)](https://github.com/KianJRoss/UltimateAssistant-release/releases/latest)
[![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue)](LICENSE)
![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20Linux-lightgrey)

A personal assistant that runs on your own computer, with its own local Herald Router. Bring any AI account you
already have: a subscription (ChatGPT, Google AI or Claude through their official CLIs), an API key, or a local
model. Your keys stay encrypted on your device; there is no account with us and no server of ours in between.

## Download: v0.4.2

- **Windows:** [UltimateAssistant-0.4.2-windows-x64.zip](https://github.com/KianJRoss/UltimateAssistant-release/releases/download/v0.4.2/UltimateAssistant-0.4.2-windows-x64.zip)
- **Linux:** [UltimateAssistant-0.4.2-linux.tar.gz](https://github.com/KianJRoss/UltimateAssistant-release/releases/download/v0.4.2/UltimateAssistant-0.4.2-linux.tar.gz)
- **Android phone app:** [UltimateAssistant-phone-1.1.0.apk](https://github.com/KianJRoss/UltimateAssistant-release/releases/download/phone-v1.1.0/UltimateAssistant-phone-1.1.0.apk) (pair it from the assistant)

Checksums are in [SHA256SUMS.txt](SHA256SUMS.txt). See the [release notes](RELEASE-NOTES.md) and
[all releases](https://github.com/KianJRoss/UltimateAssistant-release/releases).

### Requirements

| | Windows | Linux |
|---|---|---|
| System | Windows 11, x64 | A systemd-based distribution (tested on Arch/Omarchy); desktop features are built for Hyprland |
| Python | 3.13 (installed for you with Windows Package Manager if missing) | 3.13 (`python3.13`, or `mise` to install it) |
| Other | Internet access during setup | `curl` and internet access during setup |
| AI account | Any one: a ChatGPT, Google AI or Claude subscription, an API key, or a local model | Same |

### Install on Windows

1. Download and extract the **entire ZIP**.
2. Double-click `apps/assistant/Install-UltimateAssistant.cmd`. If Python 3.13 is missing, the installer gets it
   with Windows Package Manager.
3. The app opens in your browser and starts again whenever you sign in to Windows.

### Install on Linux

1. Extract the archive to a folder you keep (the assistant runs from it).
2. Run `./install.sh` in that folder. It needs Python 3.13 (`python3.13`, or `mise` to install it) and `curl`.
3. The assistant runs as a user service at login; open `http://127.0.0.1:8765` or "Ultimate Assistant" in your
   app launcher.

### First run

In the app, connect a subscription or paste an API key. A single API key is enough: the assistant picks a model
for conversation, quick tasks and heavier work from what your accounts can do. Once an account is verified, the
assistant offers to help set itself up; answer its questions and it handles the technical steps.

Setup downloads the Herald Router from [Herald's public release](https://github.com/KianJRoss/herald-release) and
checks it against a pinned checksum. Optional features (school, phone control, desktop controls, calendar,
budgeting and more) are separate downloads listed in [modules/catalog.json](modules/catalog.json) and install only
when you need them. No GitHub account, Git, or developer setup is needed.

## Optional quick decisions

Your AI model always does the real work. An optional mixed mode adds a small, fast decision model (Jev hosted, or
tev1 locally) for quick yes/no checks, such as which specialist should handle a message, only where it measured at
least 90% correct and no slower than your model.

## Updates

Updates are opt-in: public HTTPS downloads, checked by SHA-256, installed beside the current version and selected
at the next start, with rollback. Your settings and conversations stay in your own data folder.

## Diagnostics

Use *Preview and send diagnostics* to review and send one report, or *Download diagnostics* to keep it. Automatic
daily sharing is off unless you turn it on. Reports contain versions and fixed status and timing fields, not
chats, prompts, files, paths, account or model names, screenshots, credentials or raw logs.

## Source and license

This repository holds the curated source of the release bundles. Ultimate Assistant is licensed under the GNU
Affero General Public License v3.0 or later ([LICENSE](LICENSE)); releases up to 0.3.2 were published without a
license. See [SOURCE.md](SOURCE.md) for how the source relates to the downloads, the
[app documentation](apps/assistant/README.md) and the
[third-party notices](apps/assistant/THIRD_PARTY_NOTICES.md). The Kokoro speech model's Apache-2.0 license is in
`apps/assistant/models/kokoro`.

## Feedback and contributing

Report problems or suggest features in [Issues](https://github.com/KianJRoss/UltimateAssistant-release/issues/new/choose).
Keep account details, tokens, login codes and private screen captures out of public reports. Security problems
go through [SECURITY.md](SECURITY.md) instead. Pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).
