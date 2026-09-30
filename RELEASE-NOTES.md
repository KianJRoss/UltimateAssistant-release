# Ultimate Assistant 0.2.0

The Windows installer includes a separate local Herald Router. Launch the desktop shortcut to start both services on loopback. Connect Antigravity or Codex through Herald's existing login flow. The Windows Antigravity terminal uses a Windows pseudoterminal.

Kapture and Microsoft Playwright MCP are available independently or together. Install the Kapture Chrome extension and enable its connection toggle on a tab in Chrome or a Chromium browser. Playwright uses a separate profile. Vision is bundled and optional. Setup reports tool discovery failures as blocked and missing extension connections as waiting.

The assistant leads machine setup using the active provider's native tools and official instructions. Account sign-in, MFA and browser extension approval happen on the user's device. Setup conversation history persists across continuation.

Updates download from the public release repository over HTTPS, verify SHA-256, install in a separate version directory, perform an isolated import check, and retain the previous installation. Automatic checks are enabled in App updates. Git and GitHub accounts are not required on the user's device.

Internet is required for dependency installation and provider use. Model subscriptions and optional connector accounts are supplied by the user. Kapture without a connected extension tab is waiting, not a completed browser connection. Real provider OAuth and school portal access are user onboarding steps; delivery tests use synthetic data.

Official browser instructions: https://github.com/williamkapke/kapture and https://github.com/microsoft/playwright-mcp.

Provider setup also discovers Herald?s supported Claude and Antigravity profile login options. The secure API-key form stores Windows-encrypted secret references and supports OpenAI, Gemini, Anthropic, OpenRouter, DeepSeek, xAI and compatible endpoints. Saved credentials are unverified until an actual provider test succeeds.

Optional g4f 8.5.9 runs locally, with a separate worker and session directory per user-created account. ChatGPT session capture reuses Herald?s protected Kapture flow; other providers can use the native g4f interface or import their own HAR/cookie export. Imported or captured sessions require restart and a successful provider test. Provider availability, model access and subscription eligibility depend on the provider and account. No browser sessions or accounts are supplied with the installer.
