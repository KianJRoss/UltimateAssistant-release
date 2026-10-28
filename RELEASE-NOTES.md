# 0.4.0 — Any AI account, reminders that know when to speak up

- Use any AI account: a subscription CLI, an API key or a local model. The assistant picks a model for
  conversation, quick tasks and heavy work from what each connected account can do, so a single API key
  now does everything. Set your own order with `model_preferences` in settings.json.
- Reminders weigh how much something matters, whether you already said "Got it", whether you're at a
  screen, and travel time: things that matter are heard about earlier and count down to when to leave;
  routine tasks and chores never take over the screen or speak; a busy moment arrives as one summary card
  where you can tick off what's done. Pushback while gaming only happens for things that matter.
- Optional mixed Quick decisions mode: your AI model always does the work, and a small decision model
  (Jev, or tev1 locally) answers only quick yes/no checks that measured at least 90% correct and no slower,
  like which specialist handles a message. Routing now asks clear questions per specialist.
- A confidently matched saved skill with nothing to fill in runs without an extra model call.

## Linux desktop interaction


- Generate Kokoro speech locally on capable desktop nodes, reuse the loaded speech
  worker, preserve shared voice preferences, and fall back to server speech. Add
  stage timings to native status and logs for response-latency diagnosis.

- Validate microphone WAV data after stopping capture; accept PipeWire's exit
  code 1 only after an intentional stop, while rejecting empty or failed captures.
- Resolve browser dependencies on Linux as well as Windows, and offer an opted-in
  device browser without saving a fixed Chromium revision path in settings.

- Native PipeWire voice invocation uses the existing assistant's transcription,
  conversations, agents, memory and speech, with optional text access and no Pi.
- Fix browser requests through device proxies, include device context on chat,
  and expose device-local module management over the node link.
- Route satellite canvas output to the named device and respect signal/canvas/
  native voice opt-outs.

# 0.3.2 — The phone app

- Pair an Android phone (8.0 or newer): ask the assistant to pair your phone, scan the code it shows, and the phone gets the same conversation, voice and the assistant's canvas. The assistant can act on the phone when you allow it (open apps, read and use the screen, files). The phone reaches the computer through Tailscale, so it works away from home too. Get the app from the releases page; the assistant gives you the link when pairing.
- When the assistant finishes some work but forgets to write the answer, it's asked once to sum up instead of the turn failing.

# 0.3.1 — Your changes survive updates

- The assistant keeps its released core, this device's setup, and what you built apart. Updates replace only the core; your own agents, skills, scripts and modules, and what was done to set up this device, stay.
- It checks at start whether the core was changed and says so: changing the core on purpose can make updates unreliable. When it finishes setup on your device, it records each fix and re-checks them after an update.
- When something needs a change to the assistant itself, or you've built something others could use, it can write it up and, if you choose, send it to the developer after showing you exactly what would be sent.
- Guided-test results for the newer features (reminders, free time) are accepted by the support server again.

# 0.3.0 — Proactive help, modules on demand, Herald as its own product

- It keeps track of what you have to do: plans you mention in chat are noted (you'll see a "Noted" card), and you're reminded ahead of time, more insistently as things get close. When you're on something else near a deadline, it nudges you to switch.
- Ask when you're free: it checks your appointments, time blocks and outings.
- Optional features are separate downloads, installed only when needed: school and studying (flashcards with spaced repetition, Canvas or Moodle), Android phone control, Windows controls (sound, brightness, windows, power), screen vision, Windows app control, documents and research, budgeting, calendar, Obsidian and more.
- Herald, the Router the assistant runs on, now installs as its own product from its public release, each package checked against its published checksum.
- Includes everything from the 0.2.4 candidate below.

# 0.2.4 — Guided updates and optional feature feedback

- After setup, a new version offers an explanation and optional guided tests. Try features one at a time, record worked/blocked/skipped, skip a version, or defer the offer. Reopen the tour from Settings and tools.
- Separately offers diagnostics sharing with report preview, one-time send, daily opt-in, decline and snooze choices. Declining is remembered. An expanded report requires renewed consent before automatic sharing resumes.
- Reports include only fixed feature IDs/status choices alongside version and activity timings. Test conversations, free-form notes, files, screenshots and credentials are excluded.
- Windows MCP bridges gain a process-local memory cap and avoid the SDK warning-replay path that can grow without bound.

This is a friend-test candidate. Guided tests use your connected AI only when you choose to try them; results are your assessment, not an automatic success claim.

# 0.2.3 ? Friend-test update

- Preview support reports before sending. Reports now contain versions and fixed action status/timing fields; free-form labels and backend names are excluded.
- Optional daily diagnostic sharing is off by default on every installation. Enable it explicitly after reviewing the report and destination; turn it off at any time. Manual download and one-time send remain available.
- Reports go to the configured private support receiver, not GitHub issues. No chat, prompts, files, paths, account names, credentials, screenshots or raw logs are sent. The receiver sees the network IP. Turning sharing off stops future sends; it does not delete already received reports.
- Windows bundles now include the required canvas renderer and all seven optional module packages. Modules are installed only when requested.
- Keeps native Antigravity model inventory/quota fallback available and accepts recovered tool errors when the native run ultimately succeeds.
- Includes Herald's required native CLI gateway, prompt transport, device relay and delegated-job modules; checks their presence before building.

This is a friend-test prerelease, not a general availability release. Existing sign-in, voice, browser, memory, study and optional integration features remain available for testing. Provider sign-in and optional account connections happen on the user's device.

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
