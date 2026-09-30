# Ultimate Assistant for Windows

## Friend-test release: v0.2.1

[Download the Windows installer ZIP](https://github.com/KianJRoss/UltimateAssistant-release/releases/download/v0.2.1/UltimateAssistant-0.2.1-windows-x64.zip) ? [Release notes](https://github.com/KianJRoss/UltimateAssistant-release/releases/tag/v0.2.1)

This build is being sent to a friend for practical testing and feedback. It is a public prerelease, not a claim that all real-world setup paths are finished.

1. Download and extract the **entire ZIP**.
2. Double-click `apps/assistant/Install-UltimateAssistant.cmd`.
3. In the app, choose a subscription or an API key and connect your own provider account.
4. Once the account is verified, the app automatically sends **Help me set up my assistant.** Answer the assistant's questions; it handles technical setup and guides actions that need you.

The app includes its own local Herald Router. No GitHub account, Git, developer configuration, provider credentials, or test accounts are required or supplied. Windows and internet access are required. The installer provisions Python 3.13 when Windows Package Manager is available; otherwise install Python 3.13 with its launcher and retry. Dependencies are downloaded during installation.

## Browser and desktop tools

- **Kapture:** the recommended way to share a connected Chrome/Chromium tab with the assistant, with richer page context. Install its extension and connect the intended tab when requested.
- **Vision MCP:** a visual view of the screen, including an open browser, when richer browser context is unnecessary.
- **Playwright MCP:** a separate browser for full assistant-controlled browser automation.
- **Windows control MCP:** desktop input and window control after the appropriate access is enabled.

External app/account connections such as Google Workspace, Zotero, Obsidian, and school portals are optional and set up only when requested. CLI providers retain their native tools and execution capabilities; scoped Herald MCP tools supplement them.

## Updates and previous release

Updates are opt-in and use public HTTPS downloads with SHA-256 verification, separate installation folders, and rollback. User settings and conversations remain in the user's data folder.

The standard update manifest remains on [v0.2.0](https://github.com/KianJRoss/UltimateAssistant-release/releases/tag/v0.2.0). For this friend-test channel, the preview manifest is [preview-release.json](https://raw.githubusercontent.com/KianJRoss/UltimateAssistant-release/main/preview-release.json). Installing the v0.2.1 ZIP directly does not require changing update settings.

## Source and build

This repository contains the curated source used for the friend-test Windows bundle. Runtime environments, accounts, captures, databases, development probes, and maintainer-only AdminLoop modules are excluded.

```powershell
./apps/assistant/build-installer.ps1 -Version 0.2.1
```

The release ZIP bundles the licensed Kokoro speech model and voice pack. Their large binaries are distributed as release assets rather than Git source files; a source installation downloads the same upstream files and verifies their checksums when absent.

See [app documentation](apps/assistant/README.md), [release notes](RELEASE-NOTES.md), and [third-party notices](apps/assistant/THIRD_PARTY_NOTICES.md). Herald's MIT license and the Kokoro model's Apache-2.0 license are included in their component directories.

## Feedback

Report friend-test problems in [Issues](https://github.com/KianJRoss/UltimateAssistant-release/issues). Include the version, what you asked, and what happened. Keep account details, tokens, cookies, login codes, and private screen captures out of public reports.
