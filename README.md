# Ultimate Assistant for Windows

Download the installer ZIP from Releases, extract the full ZIP, and run `apps/assistant/Install-UltimateAssistant.cmd`.

The assistant includes its own Herald Router on your Windows device. Connect Antigravity, Codex or another discovered Herald subscription profile, or enter an API key in the encrypted account form, then continue the saved assistant-led onboarding conversation. You complete your own account sign-in, OAuth and MFA steps.

Kapture requires its Chrome/Chromium browser extension and connecting the intended tab. Onboarding reports waiting until a tab is connected. Playwright uses its own managed browser profile. You can use either or both. Vision is optional and disabled by default.

Updates are opt-in in the app and download public HTTPS releases with checksum verification. No GitHub account, Git or developer configuration is required. The app installs updates alongside the existing version and offers rollback; user settings and conversation data remain in your user data directory.

Windows and internet access are required. The installer provisions Python 3.13 when Windows Package Manager is available; otherwise install Python 3.13 with its launcher and retry. Dependencies are downloaded during setup. No provider credentials or personal portal configuration are included.

Optional g4f accounts run locally in separate workers. Create an account and use protected ChatGPT capture, a local HAR/cookie import, or the native g4f interface. Sessions stay unverified until a provider request succeeds. Provider and model availability depends on your account and the upstream provider.
