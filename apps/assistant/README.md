# Ultimate Assistant — Friend Build

## Linux / Omarchy device

The same assistant can use a Linux desktop while its main node and Herald run on
another machine. Run `apps/assistant/setup-linux.sh --node <router-url>` with the
existing Router connection configured. This starts the device node at login and
provides the local text interface at `http://127.0.0.1:8841`.

`ultimate-assistant` starts microphone capture; invoke it again to send. A two-minute
limit stops forgotten recordings. The request uses the existing server-side
transcription, conversation, memory and agent pipeline, then plays speech through
PipeWire. Kokoro is synthesized locally when installed, using the same shared voice
and speed settings; other providers and a missing/failed local model use the server.
The local speech model is loaded in the background when the node starts, without
recording or playing audio, and the worker is reused between replies. Set `desktop_speech` to `server`
to keep generation on the main node. `ultimate-assistant status` includes stage
timings for transcription, acknowledgement, reply, speech generation and time to first audio.
Native reply speech uses short chunks, preferring sentence boundaries, and prepares one
chunk ahead while the current audio plays. `speech_first_chunk_seconds` reports preparation
of the initial clip; `speech_generation_seconds` sums synthesis for the spoken reply.
The initial main-node probe uses a two-second connection limit and ten-second read limit
before selecting the existing device fallback. A timeout after chat submission reports an
error rather than replaying work that might already have run.
When the shared assistant sends an early conversational reply (`ack`), the device speaks
it while the agent continues working, then speaks the final result. Speech playback does
not block receiving work events; quick turns without an acknowledgement speak only once.
`ultimate-assistant cancel` discards a recording or stops
playback; it does not undo a request already submitted to an agent.
`ultimate-assistant status` reports the local interaction state, and
`ultimate-assistant text` opens the optional text interface. No Pi interface is used.

When the main node (or Herald) is unreachable, the device keeps answering in emergency mode:
`ultimate-assistant send` and the text page (`ultimate-assistant text`) hand the turn to a coding CLI
signed in on this device (Claude Code, else Codex) with a prompt explaining that it stands in for the
whole assistant here, without memory or other devices. Replies are spoken with local speech. Voice
requests need the main node to transcribe, so they say so. Each emergency exchange is queued and added
to the conversation once the main node answers again.

`ultimate-assistant point` toggles pointing mode on the canvas overlay: mark a point (click),
a box (drag), a freehand stroke or a whole window (tools 1-4), then Enter (or the same key) to
keep the marks or Esc to drop them. Pointing mode also opens while a voice recording runs, so
you can point while talking. Marks go to the main node with a screenshot that has them drawn in,
and the device's next turn carries them: their description is in the turn's context, and the
assistant sees the screenshot with `canvas_marks`. An agent can also ask with `canvas_point`
("show me which one") and gets the marks back directly.

`ultimate-assistant teach` starts teach mode: show the assistant a task once, then run it again
(or press Stop on the overlay's "Recording" badge) to send it; `ultimate-assistant teach discard`
drops it, and `ultimate-assistant teach start "what it is for"` adds a note. It records the windows
used, the controls used by name (AT-SPI: focus, presses, toggles, text typed into a field; password
fields only as hidden) and a screenshot after each step, never raw keystrokes or coordinates, for at
most 15 minutes. The device's next turn mentions the recording, and an agent learns it with
`learn_from_recording`: it generalizes the steps into a skill with parameters for whatever could
differ next time, tests it, and confirms with you. Apps publish their controls to AT-SPI once
accessibility is on (teach mode turns it on); an app started before that may need a restart.

On Omarchy, bind the invocation in `~/.config/hypr/bindings.lua`, for example:

```lua
o.bind("SUPER + A", "Talk to Ultimate Assistant", "~/.local/bin/ultimate-assistant")
o.bind("SUPER + ALT + P", "Point things out to Ultimate Assistant", "~/.local/bin/ultimate-assistant point")
o.bind("SUPER + ALT + T", "Teach Ultimate Assistant a task", "~/.local/bin/ultimate-assistant teach")
```

Unbind an existing shortcut before replacing it. Desktop vision, Hyprland control
and environment file/shell tools remain device-local modules enabled by
`desktop_vision`, `desktop_control`, and `environment_access` in
`~/.ultimate-assistant/UltimateAssistant/settings.json`. Signals, canvas and native
voice can each be disabled with `signals`, `canvas_overlay`, or `desktop_voice`.
The microphone is opened only on explicit invocation. Global microphone/speaker
selection follows PipeWire defaults; Voxtype dictation remains separate.

Set `playwright_enabled` to `true` on a device to offer the setup-installed
Playwright browser over its node link. Node and Chromium are resolved for the
current platform on each registration, so dependency updates do not leave a saved
Chromium revision path behind. An explicit `device_mcp_servers.playwright` entry
takes precedence, including a disabled entry. This separate browser has its own
profile; Kapture still requires an explicitly connected user browser tab.

## Windows setup

Voice-first assistant UI using Herald's Router and Package. The friend interacts
through the assistant UI rather than Herald's developer-oriented Pi CLI. The
Router selects model backends. CLI providers run through their full native
harnesses and tools, supplemented by scoped Herald MCP tools; non-CLI providers
use Herald's bounded agent harness.
Provider CLIs can run as model backends behind the UI using their own logins and
tool capabilities; the friend does not operate their terminals. Tool actions
use the configured Herald project/part scope and respect backend/tool settings.

## Setup

Extract the complete friend-build ZIP and double-click
`apps\assistant\Install-UltimateAssistant.cmd`. The installer prepares the local
Router and tool dependencies. Connect an AI subscription or API key in the app;
after verification, the app automatically sends **Help me set up my assistant.**
The assistant asks simple questions, handles technical configuration, and guides
the user through necessary permissions and browser connections.

Core setup covers browser automation, vision, and Windows control. Connections
to external apps and accounts are optional and set up only when requested.

- **Kapture:** share a connected Chrome/Chromium tab with richer page context; this is the recommended option for the user's own browser.
- **Vision MCP:** view the screen visually, including an open browser, when full browser context is unnecessary.
- **Playwright MCP:** use a separate browser fully controlled by the assistant.
- **Windows control MCP:** interact with desktop applications after the appropriate access is enabled.

To build the friend-install ZIP from this development checkout, run
`./build-installer.ps1`. Extract the generated `dist\UltimateAssistant-*-windows-x64.zip`
and launch `apps\assistant\Install-UltimateAssistant.ps1` from PowerShell. It
copies only the assistant, Herald Python package, Vision MCP, and desktop-control
MCP into the per-user app install directory, creates isolated Python 3.13
environments, and runs first-time Router configuration. It does not copy Git
history, maintainer-only AdminLoop modules, credentials, databases, unrelated
projects, or the developer's venvs. This is a Windows friend-test release,
not a claim that every provider or integration has completed real-world testing.

Setup requires Python 3.13 for all three isolated Python environments. Node.js/npm enables the official
filesystem server. These are CPU-friendly local components, but OCR and speech
use more memory than text-only chat.

`configure.ps1` verifies the Router URL and API key against `/v1/models` before
writing either value; a failed connection leaves the previous configuration
unchanged. Herald, the Router, is its own product: setup installs the release
pinned in `herald.json` and checks its SHA-256 (or, for development, the
checkout named by `HERALD_SOURCE_ROOT`). The friend supplies their own
provider account.
The configurator also chooses a local work folder, defaulting to
`Documents\UltimateAssistant`. On a same-device Router, the assistant binds the
official filesystem MCP and Herald's shell MCP to that folder, providing
scoped file read/write/search and command tools. The filesystem server uses
Node.js/npm (`npx`); install Node.js if the UI reports that server unavailable.
Change the selected folder with `ULTIMATE_ASSISTANT_FILES_ROOT` or the
`files_root` setting. The filesystem MCP confines its file operations to that
folder; Herald's shell MCP starts there but is not an operating-system sandbox.

Router URL, API key, and model preference are per-user under
`%LOCALAPPDATA%\UltimateAssistant`. The default Router is
`http://127.0.0.1:8790`; no machine or account is hard-coded. The app uses the
`friend-assistant` / `conversation` Herald scope by default. Bind only that
friend's integrations there. `ULTIMATE_ASSISTANT_PROJECT` and
`ULTIMATE_ASSISTANT_PART` can override the scope; `HERALD_MODEL` overrides the
saved model selection.

Any account works the same way: a subscription CLI, an API key, or a local model.
The assistant picks a model for each kind of work by what Herald says it can do
(`fast` for conversation and quick tasks, `code`/`reasoning` for heavy work), then
the selected model, then every other connected account by priority. With a single
API key, that key does everything. To set your own order, add
`"model_preferences": {"conversation": [...], "light_work": [...], "heavy_work": [...]}`
(model ids) to `settings.json`.

Conversation history, durable cross-conversation memories, and a small
tasks/commitments ledger are stored in local SQLite databases under
`%LOCALAPPDATA%\UltimateAssistant\data`. Open tasks are
included as reference context for chat; users can add and complete tasks in the
UI, including study-recovery follow-ups with suggested minutes. When the Router
is on the same device, the app binds a project-scoped MCP task tool so the
assistant can add/update commitments conversationally. After it substitutes
for learning to complete an urgent deliverable, its policy is to explain the
learning gap and record a proportionate, reschedulable study-recovery task.
With a remote Router, tasks remain visible in the app but that local task tool
is not registered. The read-only portal explorer can add assignments to the ledger
when an explicit due date and matching page evidence are verified.

When the Router is on the same device, the assistant also binds Herald's
project-scoped math MCP. It provides symbolic algebra and calculus, curve
analysis and plotting, and chemistry calculations. Setup installs SymPy,
Matplotlib, and NumPy (Herald's optional math dependencies) in the assistant
Python environment so the tool process has its runtime dependencies. For schoolwork, chat can make
active-recall questions, flashcards, and step-by-step worked examples from
material the user provides, and can help plan study sessions around tasks,
deadlines, and the user's available time. At the user's request, study sessions
can be saved, started, and completed in a separate local SQLite ledger; this
does not schedule OS notifications or reminders. On request, the assistant can
also build persistent flashcard decks from provided material, quiz due cards,
and schedule the user's self-ratings with a simple Leitner review interval.
The compact Math graphing panel renders local plots and curve-analysis summaries.
These learning features do not
require a school account or portal; the assistant should ask rather than infer
missing course context or availability.

Durable memory is separate from conversation summaries and is shared across new
chats on this Windows account. It stores explicit rules/preferences, ongoing
context, and useful past episodes; the assistant retrieves relevant entries
before answering, while the full transcript remains the source record. Users
can add and forget memories in the Memory panel; clearing a conversation does
not erase them. Same-device Herald exposes project-scoped memory search/save/
forget tools to supported model harnesses and the optional CLI bridge. If
`ULTIMATE_ASSISTANT_EMBEDDING_MODEL` is configured, embeddings use the selected
Ollama embedding model at `http://127.0.0.1:11434`; otherwise the assistant uses
local keyword matching. No external embedding service is contacted by default.
Importance and recency affect retrieval, not retention: memories are not
silently erased as they age.

The Connected Herald tools panel displays integrations discovered in the app's
scope. Herald's chat harness resolves both legacy part bindings and reusable
MCP-group bindings, so tools shown in the assistant scope are also available to
the model. The friend can choose an available model in the UI; model and tool
delegation stay behind the Router.

The selected provider CLI's own tools are the primary local-work interface.
Codex, Antigravity, or another configured native CLI can inspect official instructions, install a
compatible MCP, guide the user through OAuth or other account-specific steps,
and verify the result. The connector catalog is onboarding guidance, not a
whitelist or prerequisite. Herald remains the orchestration layer for swarms,
loops, and optional MCPs the user wants shared in this assistant's project
scope. Do not require a duplicate Herald integration when the CLI's own tools
already complete the task, and do not claim setup succeeded until its tools are
discovered and exercised.

At runtime, CLI backends run agentically through their native CLI tool loop;
non-CLI backends use the Router's full harness. A CLI sees Herald MCP tools only
after its own MCP configuration points to `herald.mcp_gateway` with this
assistant's project/part scope. The assistant is instructed to configure that
bridge using the selected CLI's supported setup flow when a task needs it, then
verify it on a fresh turn. The gateway exposes project/part-scoped Herald MCP
tools and recurring agentic schedule tools for listing, creating, disabling,
and reviewing scheduled loops. These loops are not AdminLoop's specialized
tool-using Git swarm; use the CLI's native subagents for tool-using parallel
work unless a dedicated Herald swarm tool is explicitly available.

The **Connect apps and MCPs** panel is the assistant's connector onboarding
catalog. It explains the user's steps for Google Workspace OAuth, Zotero API
access, and Obsidian's Local REST API plugin; its Ask button starts a guided
conversation and the model is told not to claim a connector is installed unless
live tools confirm it. Zotero now has an in-app setup form: the app checks the
user ID and API key with Zotero, stores the key in Herald's encrypted local
vault, and registers a read-only search/collections/item MCP in the assistant's
project scope. It reports connected only when Herald discovers the tools. Use a
personal read-only Zotero key; it never enters the model prompt. Google
Workspace and Obsidian remain guidance-only while their OAuth/plugin flows and
portable MCP registration are built. A local config file alone is not proof of
authorization. Do not paste credentials into chat.

Desktop Vision/OCR is bundled and configured during guided core setup on a
same-device Router; the user can disable it in Connected Herald
tools. It does not continuously capture the screen; the agent calls its tools
when useful. A remote Router cannot capture the local desktop, and local VLM
inference requires a configured Ollama model or optional vision API key.
Desktop control remains separately opt-in; enabling it registers the bundled
MCP and binds its group only to this assistant's project/part, while disabling
it removes the scope binding. Setup installs the MCP dependencies. Desktop
control can focus windows, type, send
hotkeys, and launch/close supported apps. There is no extra per-action app
confirmation layer: the assistant follows the user's chosen security settings
and the connected tool's actual restrictions. A remote Herald Router cannot
launch these local stdio servers, so desktop capabilities require the Router
and app on the same laptop.

## Voice

The UI binds only to `127.0.0.1`. Text chat works without audio. The microphone
picker refresh requests permission to list devices; push-to-talk records from
the selected device for up to two minutes, then sends audio to the configured
Router's `/voice/transcribe` endpoint. Press `Alt+Space` or tap the mic to start
or stop. Wake-name listening is optional, uses browser-managed speech
recognition, may process audio externally, and may use the system-default mic.
Voice preferences and the active conversation ID are stored in browser local
storage. Full chat transcripts are stored in a per-user SQLite database at
`%LOCALAPPDATA%\UltimateAssistant\data\conversations.sqlite3`; this is local
plaintext storage, not Herald cloud memory. The UI's Clear chat action deletes
the active conversation and its summary. When sufficient older history
accumulates, the selected Herald model creates a source-message-ID-linked
carry-forward summary; the UI exposes it for review. The original transcript
remains unchanged in SQLite. If compaction fails, the app keeps unsummarized
history in model context rather than discarding it. This is continuity memory,
not semantic search over past conversations.

The chat attachment control extracts text locally from TXT, Markdown, CSV/TSV,
JSON, HTML, XML, PDF, DOCX, PPTX, XLSX, and common image files. Each file is limited to 15 MB;
the app accepts up to five attachments and sends at most 50,000 extracted
characters per turn to the configured Herald model. Original files are not
uploaded or retained by the app, but extracted contents are included in the
local conversation database and transmitted to the selected model. File
contents are treated as untrusted references. Images use Herald's local OCR
ingestion; full visual description is available only when its optional VLM is
configured. Scanned-PDF OCR and legacy Office formats are not supported by this
attachment path. The Router currently flattens multimodal message content, so
images reach the model as extracted OCR/description text, not as raw pixels.

Speech output supports ElevenLabs, bundled local Kokoro ONNX, and browser device
voices. ElevenLabs credentials remain on the local app server, but reply text is
sent to ElevenLabs and may consume quota. Errors fall back to Kokoro and then
the browser voice. Setup installs a separate Python 3.13 Kokoro environment and
downloads model/voice assets into `apps/assistant/models/kokoro`; include those
assets when packaging. Local inference works offline after setup.

## School portal discovery

The UI's portal panel opens a temporary headed Edge session. The user signs in
and completes MFA directly in Edge; credentials and browser state are not saved
by the assistant. Inspect sends visible page text to the selected Herald model.
AI explore lets the model choose among numbered, same-origin links for up to six
observed pages. It can finish with evidence or stop and ask the user for login,
MFA, or clarification. Form values are excluded, query strings are stripped,
and page text is not added to persistent agent-session memory. The browser
agent cannot fill forms or perform write actions. When it finds an assignment
with an explicit ISO date and a quote verified against visible page text, that
assignment is deduplicated and added to the local task ledger; grades remain
evidence-only. This is still read-only browser discovery, not a persistent
course database connector.
Use **Open fake school** to try the flow against local-only synthetic courses,
assignments, and grades without an institutional account.

## Recovery behavior

Herald-backed chat instructs the selected CLI or harness to diagnose tool
failures, adapt calls, verify outcomes, and try a few targeted recoveries before
reporting a blocker. It must not modify Herald's installation, provider
credentials, account security, or unrelated files to repair a task. The portal
explorer records failed navigation, refreshes observations, and replans around
the failure, with a bounded page/action budget. These are agent policies and
workflow limits, not a guarantee that every environment problem can be repaired
automatically; human MFA, missing permissions, and service outages remain
blockers.

## Local knowledge and privacy

The app does not load College Assistant OAuth/browser-state files, rebuild its
indexes, or use its personal local index by default. Its independent index is
under `%LOCALAPPDATA%\UltimateAssistant\data`. Setting
`COLLEGE_ASSISTANT_ROOT` explicitly opts into that project's index. The CLI
web-search fallback finds Herald's `websearch_tools.py` in the installed Herald
package, or under `HERALD_SOURCE_ROOT` for a development checkout.

## Reopening, sign-in startup, and voices

New installations open at Windows sign-in for the installing user. Existing
installations register this on their next launch after updating. The desktop
shortcut still reopens the app; duplicate launcher starts reuse its browser UI.
Run `configure-startup.ps1 -Disable` to opt out or `configure-startup.ps1` to
turn sign-in launch back on. Installer `-SkipStartup` also opts out. Isolated
preview/update installations using `ULTIMATE_ASSISTANT_NO_SHORTCUT` do not
change the host user's startup registration.

Voice choices, speaking speed, speak-replies preference, and wake name are saved
under `%LOCALAPPDATA%\UltimateAssistant\voice-preferences.json`. Existing browser
preferences migrate on first load. This state survives app updates and a different
browser on the same Windows account. Kokoro detects its installed local runtime
without requiring a custom environment variable; installation supplies its model
and voice pack. ElevenLabs uses each user's own API key and available account
voices. Device voices depend on voices installed in Windows. This does not copy
credentials or synchronize preferences between separate computers.

## Your changes and updates

The assistant keeps three things apart so updates can replace it without losing anyone's work:

- **The core** is the app as released. Updates replace it whole, and the app checks itself
  against the release's file list (`core-manifest.json`) at start. **Changing the core on purpose
  can make updates unreliable**: those edits are lost on the next update and may stop it working.
- **This device's setup.** Every install is a little different (where things are, drivers, what
  was already installed), and the assistant finishes whatever the installer couldn't. What it did
  is recorded with how to re-check it, kept across updates, and re-checked after one.
- **What you built or taught it**: your own agents, skills, scripts, automations and modules, in
  `agents/`, `skills/` and `extensions/` in its user data folder. Updates never touch them.

When something only a core change would fix, or you've built something others could use, the
assistant can write it up and, **if you choose**, send it to the developer. You see exactly what
would be sent first; nothing else (conversations, files, settings, accounts) goes with it. Sending
fixes back is how they reach everyone, instead of living on in one changed copy.

## Diagnostic sharing

Assistant activity stays in each conversation as timestamped actions and statuses,
including completion and failure. Download diagnostics creates a ZIP containing
version, backend and activity timings. Send diagnostics uploads the same ZIP to
the support destination provided by the release channel. Reports exclude chat
text, attachments, employee records, credentials and raw provider logs. Upload is
explicit, not automatic; no remote-control connection is established.

A custom support destination can be set in the user's `diagnostics-upload.json`
with an HTTPS `url` and optional vault `token_ref`. The receiver accepts a ZIP body
with `Content-Type: application/zip`. Failed uploads remain downloadable.

Antigravity quota fallback reads the native `/usage` groups and `models` inventory
only after a quota error. It keeps the same native harness and account, tries up
to two models with remaining allowance, and reports switches in activity. Native
tool activity prevents automatic replay of a request. Unknown limits are not
presented as remaining allowance.

## Decisions

Small choices (routing, needs-tools, topic shift, skills, commitments, lane, acknowledgement, goodbye) use one typed interface with an `llm` route (default) and an opt-in `decision_model` route. See [DECISIONS.md](DECISIONS.md).
