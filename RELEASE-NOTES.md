# 0.2.2 ? Friend diagnostics update

- Timestamped assistant action history remains visible after completion and after reopening a conversation.
- Diagnostic download and explicit upload include version, backend and action timings, excluding chat text, credentials and raw provider logs.
- Support upload destination is supplied by release-channel metadata or per-user configuration.
- Launch at Windows sign-in, with an opt-out and duplicate-launch protection.
- Voice preferences persist in user data; installed Kokoro runtime is detected automatically.
- Antigravity can fall back to an available model in another quota group after a confirmed quota failure, before native tool actions have started.
- Simple device actions are directed to connected tools immediately; independent complex work uses available native subagents or a verified swarm, and ongoing requests use available scoped loops.

A diagnostic report is sent only when the user presses Send diagnostics. This update does not provide remote control of the user's machine.

# Ultimate Assistant 0.2.1 ? friend-test release

This release is intended to be sent to a friend for practical testing and feedback.

- Onboarding first asks whether to use a subscription or an API key, discovers available providers, guides login, and verifies the account.
- After verification, the app automatically sends **Help me set up my assistant.** The assistant asks questions, performs technical setup with its native CLI tools, and guides necessary user actions.
- Core setup covers browser automation, vision, and Windows control. External app/account connections are optional and set up only when requested.
- CLI providers retain their full native harness and tools. Scoped Herald MCP tools extend them. Unattended calls use the installed providers' supported full-permission flags.
- Antigravity responses are assembled correctly. Empty replies and tool failures are reported with native diagnostics and useful partial progress retained for continuation.
- Saved connection changes can trigger one automatic fresh native turn for verification.

## Browser and screen tools

- **Kapture** is the recommended option for sharing a connected Chrome/Chromium tab with the assistant, including richer page context. Install its extension and connect the intended tab when requested.
- **Vision MCP** provides a visual view of the screen, including an open browser, when richer browser context is unnecessary.
- **Playwright MCP** provides a separate browser for full assistant-controlled browser automation.
- **Windows control MCP** provides desktop interaction tools. The assistant guides enabling the appropriate access.

## Current test scope and limitations

The packaged Windows installation completed in isolated user and installation folders. Regression checks passed. Native provider calls, shell tools, and scoped MCP calls were exercised in the private preview. This is an initial friend-test release; upstream provider/network errors, protected native configuration, and MCP discovery/session failures can pause a setup turn. These failures are reported and progress retained, rather than treated as completed setup. A provider subscription, account permissions, browser extension connection, and any optional app authorization remain user-specific. No accounts, credentials, test state, or developer environments are supplied.

Public HTTPS updates use checksum verification and keep previous installations available for rollback. The previous v0.2.0 release remains available.

# Ultimate Assistant 0.2.0

The Windows installer includes a separate local Herald Router. Launch the desktop shortcut to start both services on loopback. Connect Antigravity or Codex through Herald's existing login flow. The Windows Antigravity terminal uses a Windows pseudoterminal.

Kapture and Microsoft Playwright MCP are available independently or together. Install the Kapture Chrome extension and enable its connection toggle on a tab in Chrome or a Chromium browser. Playwright uses a separate profile. Vision is bundled and optional. Setup reports tool discovery failures as blocked and missing extension connections as waiting.

The assistant leads machine setup using the active provider's native tools and official instructions. Account sign-in, MFA and browser extension approval happen on the user's device. Setup conversation history persists across continuation.

Updates download from the public release repository over HTTPS, verify SHA-256, install in a separate version directory, perform an isolated import check, and retain the previous installation. Automatic checks are enabled in App updates. Git and GitHub accounts are not required on the user's device.

Internet is required for dependency installation and provider use. Model subscriptions and optional connector accounts are supplied by the user. Kapture without a connected extension tab is waiting, not a completed browser connection. Real provider OAuth and school portal access are user onboarding steps; delivery tests use synthetic data.

Official browser instructions: https://github.com/williamkapke/kapture and https://github.com/microsoft/playwright-mcp.

Provider setup also discovers Herald?s supported Claude and Antigravity profile login options. The secure API-key form stores Windows-encrypted secret references and supports OpenAI, Gemini, Anthropic, OpenRouter, DeepSeek, xAI and compatible endpoints. Saved credentials are unverified until an actual provider test succeeds.

Optional g4f 8.5.9 runs locally, with a separate worker and session directory per user-created account. ChatGPT session capture reuses Herald?s protected Kapture flow; other providers can use the native g4f interface or import their own HAR/cookie export. Imported or captured sessions require restart and a successful provider test. Provider availability, model access and subscription eligibility depend on the provider and account. No browser sessions or accounts are supplied with the installer.

Restart app and Quit app controls stop the app-owned Router cleanly. Restart loads an installed update while preserving conversations and accounts. Existing app desktop shortcuts are refreshed on launch.
