# Herald

Herald gives every AI model and tool you already use one dependable front
door. Connect hosted APIs, local models, browser sessions, MCP servers, and
coding CLIs such as Claude Code and Codex; Herald handles selection, policy,
failover, usage, and tool access behind a consistent interface.

## One router, three ways to use it

| Part | What it does | Best for |
| --- | --- | --- |
| **Herald Router** | Connects models, accounts, coding CLIs, and MCP tools. It applies routing modes, health checks, budgets, project scopes, and failover in one control plane. | Giving a team or application one stable AI endpoint instead of integrating every provider separately. |
| **Herald CLI** | Provides focused terminal commands plus a full-screen, Pi-based agent interface. Agents can code, handle everyday work, call scoped tools, and delegate to other models. | Interactive work, repository tasks, automation, and multi-agent workflows. |
| **Herald Package** | Exposes simple helpers and durable agents through ordinary Python, with a TypeScript router client for web and Node applications. | Fast, contextual decisions that are too ambiguous for a normal algorithm but too frequent to send through a manual workflow. |

The Router is the shared foundation. The CLI and Package are independent
clients of it, so a model, account, or tool configured once can be used from a
terminal, an application, or a project-scoped agent without duplicating
integration code.

### Where Herald fits

- Use normal code for deterministic rules.
- Use Herald Package helpers for classification, extraction, summarization,
  review, and bounded decisions that need language or context.
- Use Herald CLI agents when the work is exploratory, tool-heavy, or spans
  several steps.
- Use Herald Router when you need the same models, tools, policies, and usage
  controls across all of those workloads.

## Get started

Install the Python distribution and configure the Router:

```powershell
pip install herald-ai
herald setup
herald status
```

For an unreleased source checkout, use `pip install -e .` instead.

Run a one-off task or enter the interactive agent interface:

```powershell
herald ask "Summarize the changes in this repository"
herald code
herald code --print --prompt "Review this repository"
```

Or put a small, validated AI decision directly in application code:

```python
from herald import classify

queue = classify(
    support_ticket,
    labels=["billing", "bug", "question", "needs-review"],
    mode="efficiency",
)
```

Herald validates the result against the allowed labels and routes the request
through the configured model pool. Higher-stakes workflows can use a stateful
agent with a confidence threshold and a human-review fallback.

The Router starts locally at `http://localhost:8790`. Its Account Console at
`http://localhost:8790/ui` manages accounts, secret references, CLI logins,
connection health, usage, and orchestration flows.

## Choose a path

- [Deploy the Router](docs/DEPLOYMENT_GUIDE.md) on one machine or across a
  private model mesh.
- [Use the CLI](docs/CLI.md) for commands, the Pi-based interface, agents, and
  automation.
- [Embed the Package](docs/PACKAGE.md) for helpers, durable decisions, and
  application integration.
- [Prepare a public release](docs/PUBLICATION_ROADMAP.md) with the complete
  security, registry, automation, and rollout gates.
- [Follow the development guidelines](docs/DEVELOPMENT_GUIDELINES.md) and
  [publishing policy](docs/PUBLISHING_POLICY.md) for future changes.
- [Explore the public Python API](docs/PUBLIC_API.md) or the
  [component reference](docs/COMPONENTS_REFERENCE.md).

## Accounts and model lanes

Named accounts distinguish independent login sessions from the model lanes
they expose. This supports primary/backup CLI profiles, several G4F browser
sessions, multiple API keys, and accounts such as Antigravity that expose
Gemini, Claude, and GPT lanes:

```powershell
herald account add openrouter-work --provider openrouter --kind api_key `
  --secret-ref env:OPENROUTER_WORK_KEY `
  --config '{"model_name":"openai/gpt-4.1","pool_name":"openrouter-work"}'
herald account activate openrouter-work
herald account lane openrouter-work reasoning --backend reason `
  --capability reasoning=true
```

Secret values are never accepted in account configuration. Accounts store an
`env:`, `keyring:`, or `command:` reference instead. Activating an API-key
account creates a callable backend that still contains only the reference;
the adapter resolves the key at request time and never writes the resolved
value into Herald's databases or API responses.

The Account Console can test a selected connection, inspect imported CLI
connections, and register Kapture. Interactive coding and flow authoring stay
in the CLI or Package; the console configures and monitors the Router.

## Routing behavior

Every API, SDK, flow, and CLI call can select a behavior independently:

- `efficiency` (default) automatically uses free browser-session or local
  models and excludes subscription CLIs/paid APIs from autonomous delegation.
  Explicitly naming a paid model still honors your choice.
- `balanced` allows every healthy backend while respecting configured
  priorities.
- `quality` prefers CLI subscriptions and paid API backends.
- `local` restricts automatic work and delegation to local models.

G4F does not need native function calling. In agentic mode it emits Herald's
provider-neutral `TOOL_CALL` protocol; the router validates the call against
the project scope, executes the MCP tool, and returns the observation. In
efficiency mode tool descriptions are compacted before prompting, reducing
context overhead while keeping the full schema at the trusted execution
boundary.

## Multi-model orchestration

`herald init` creates `.herald/flows/feature.yaml`. A flow separates topology
from behavioral rules and gives every named agent an independent memory cache.
The same model can therefore be instantiated several times with different
context, or several agents can deliberately share one cache.

```yaml
version: 1
name: feature
mode: efficiency
agents:
  Director: {model: auto, memory: director}
  Researcher: {model: g4f-gateway, memory: research}
  Reviewer: {model: auto, memory: review}
flow:
  - Director
  - [Researcher, Reviewer]  # parallel stage
  - from: [Researcher, Reviewer]
    to: Director            # same Director remembers its first invocation
  - output
rules:
  Director: Own the plan and final answer.
  Researcher: Gather concise evidence with free tools.
  Reviewer: Find defects and missing tests.
```

The YAML is optional configuration, not a new programming language. Projects
can use the conventional object-oriented Python API instead:

```python
from herald import Flow, Harness

flow = Flow("feature", mode="efficiency")
director = flow.agent("Director", "auto", memory="director")
research = flow.agent("Researcher", "g4f-gateway", memory="research")
review = flow.agent("Reviewer", "auto", memory="review")
flow.then(director).then(research, review).then(director).then("output")
flow.rule(director, "Own the plan and final answer.")

result = Harness.open(".").run_flow(flow, "Implement the next feature")
print(result["content"])
```

Validate without executing models:

```powershell
herald flow validate .herald/flows/feature.yaml
```

Add `--persist` to encrypt the flow definition, input, independent agent
memories, stage checkpoints, and final result. SQLite keeps only operational
metadata and opaque vault references. Interrupted runs resume after their last
completed stage:

```powershell
herald flow runs
herald flow resume <run-id>
```

Flow lifecycle events (`flow.run.started`, `checkpoint`, `completed`,
`failed`, and `resumed`) are sanitized before storage. Opt-in hooks can forward
them to an HTTP endpoint or an argv-based local command:

```powershell
herald hook add notify --event "flow.run.*" --url https://example.test/herald
herald events --limit 20
herald hook remove notify
```

## Kapture and external CLI connections

### Controlled MCP groups

Herald owns one shared MCP server library. Named groups select whole servers or
allowlist individual callable tools, and bindings make those groups visible at
four levels: every Herald context, one project, one project part, or one named
external CLI profile. Existing direct project-part bindings continue to work
and are combined with matching groups.

```powershell
# Register pinned native integrations and collect them into a group.
herald mcp native add filesystem --root . --group coding
herald mcp native add shell --root . --group coding
herald mcp native add github --group collaboration

# Restrict a large server to selected callable tools when desired.
herald mcp group add coding filesystem --allow read_file --allow search_files

# Assign reusable groups at different levels.
herald mcp group bind coding global
herald mcp group bind coding project my-app
herald mcp group bind coding part my-app/backend
herald mcp group bind collaboration cli codex-primary
herald mcp group bind collaboration cli claude-primary

# Inspect the exact catalog before connecting an agent.
herald mcp group list
herald mcp access --profile codex-primary
herald mcp access --project my-app --part backend
```

The filesystem entry uses the pinned official Model Context Protocol server.
The shell entry is Herald's bundled protocol-correct Bash/PowerShell server;
its configured root controls the working directory but is not an operating
system sandbox. The GitHub entry downloads GitHub's pinned official release
binary and verifies it against the release checksum before installation.
When `GITHUB_PERSONAL_ACCESS_TOKEN` is absent, `herald mcp native add github`
can reuse the active `gh` login and store the token in Herald's encrypted
vault without printing it.

Claude Code and Codex connect to the same Herald gateway while retaining
different group assignments:

```powershell
herald mcp connect codex --profile codex-primary
herald mcp connect claude --profile claude-primary
```

Use `--force` only when intentionally replacing an existing connection named
`herald`. The gateway command itself is `herald mcp serve`; external CLIs
normally receive it through `herald mcp connect`.

Herald can inventory MCP connection names from Claude Code/Desktop, Codex,
Gemini, Qwen, and Antigravity without returning their command environments or
secrets. The dashboard can register the pinned MIT-licensed
`kapture-mcp@2.6.1` bridge as a global tool. Kapture provides shared Chromium
tab control and network request/body capture on port `61822`.

Herald includes a provider-specific, no-model G4F capture path. It watches a
real ChatGPT conversation request through Kapture, requires both the bearer
and Sentinel proof fields, verifies the token's masked identity hint against
the selected G4F account, encrypts the resulting session artifact, and only
then atomically materializes the restricted working HAR. Captured headers,
Kapture tab IDs, cursors, vault references, and filesystem paths are not
returned by the inventory APIs or dashboard.

```powershell
herald capture accounts
herald capture start burner1
# Send one short message in the connected ChatGPT tab when prompted.
# Restart the G4F account stack after a successful materialization.
```

Generic cross-provider cookie export remains disabled: each provider needs an
explicit adapter and identity verifier before Herald will write its session.
On Windows the vault master key is protected with DPAPI when available. On
headless systems, set `HERALD_VAULT_KEY` to a Fernet key (recommended for a
service); Herald otherwise tries the OS keyring and finally a mode-0600 local
key file.

External MCP connections can be previewed and imported from known Claude,
Codex, Gemini, Qwen, and Antigravity config locations. Imports move literal
environment/header secrets into Herald's vault, preserve `${ENV_VAR}` as an
environment reference, and reject credentials embedded in URLs or command
arguments.

```powershell
herald integration list
herald integration import claude my-server --scope global
herald integration import codex project-browser --scope project --project my-app
```

`herald code` launches Herald's full-screen coding environment. The managed
runtime receives Herald's models, account-aware pools, scoped MCP tools, and
`herald_delegate` recursive model graph. There is one Herald command surface,
including login, model selection, session controls, tools, and automation.
Global and project command templates are loaded from
`~/.herald/commands` and `.herald/commands`; existing `.claude/commands` and
`.codex/prompts` directories are also recognized.

The managed shell assets refresh on every launch, so upgrading Herald cannot
leave an older command/UI bundle in the isolated runtime. Start the environment
with `herald` or `herald code`, then use `/help` for the command center:

```text
/status                       router, auth, usage, G4F, and scope health
/model [MODE]                 explained behavior-mode selector
/model backend [NAME]         advanced individual-backend pinning
/accounts [activate NAME]     named CLI/API/browser/local identities
/login [CLI|refresh|code ...] login and session maintenance
/login provider [NAME]        native provider OAuth, under the same hierarchy
/capture [ACCOUNT|list]       protected G4F Kapture refresh
/appearance                   themes, holiday palettes, syntax, and thinking
/usage                        CLI, API, G4F, and local-model usage coverage
/tools [filter|run NAME ...]  scoped inventory and direct execution
/flow [list|resume RUN_ID]    persistent multi-model runs
/integrations                 external CLI MCP discovery/import
/events                       sanitized lifecycle events
/hooks                        event-hook inventory
/dashboard                    Account Console endpoint
```

`herald dashboard` opens the Account Console in the local default browser. Inside the
coding environment, `/dashboard open` does the same. Remote SSH sessions show
the URL instead so the browser is opened through the user's tunnel or
Tailscale connection rather than on the server.

Herald includes mobile workspace support accessible from any browser on your network
or private mesh (such as Tailscale). It attaches to a persistent session, so closing the
phone browser does not end the coding session. The Account Console is available at
`http://<your-router-host>:8790/ui` and mobile workspace at `http://<your-router-host>:8790/mobile`.

For phones, the coding client at `/mobile` provides a responsive chat workspace over
Herald's persistent agent memories, project-scoped filesystem, shell, GitHub, and MCP
tools. Long tasks return a durable run ID immediately, continue after the browser sleeps
or disconnects, and expose reconnectable progress for backend attempts, failovers, model
iterations, delegations, and tool calls. `/runs` shows recent work. The draggable `/`
sheet fills commands into mobile textareas. `/reload` schedules a router reload outside
the router process after active work finishes.

Native Claude, Codex, OpenCode, Antigravity, Herald, and shell TTY sessions
remain available at `/mobile/terminal`. That advanced page uses atomic tmux
paste to avoid xterm/Gboard composition corruption, but ordinary coding work
does not require entering the terminal.

Multi-model delegation rows use stable participant accents (for example,
Claude, Codex, Gemini, GPT, browser-session, and local-model families each get
a distinguishable tone). The collapsed row shows the director/agent tree;
expanding it shows model-to-model calls and returned summaries without
claiming access to reasoning text a backend did not expose.

`/usage` and `herald usage` combine router telemetry for every backend with
the native subscription limits shown by Codex and Claude Code, Claude
transcript totals, primary/backup Codex profiles, G4F account health, known
API costs, and local-model calls. Codex is read through its local app-server
account protocol; Claude is read through the same authenticated OAuth usage
source as its `/usage` screen. Herald shows used/remaining percentages and
reset times, keeps credentials in memory only, caches provider reads briefly,
and marks limits `not_exposed` when an upstream does not publish them.

Slash commands render branded, non-model command cards in the transcript;
their results are not turned into prompts. Argument completion is supplied for
models, accounts, tools, authentication targets, and capture accounts. Herald
also provides `/fork`, `/tree`, `/compact`, `/resume`, and `/quit` as its
session controls.

Herald does not pre-approve its workspace runtime. PAL's shipped Claude,
Antigravity, Codex, and other profiles likewise do not add provider permission-
bypass or sandbox-bypass flags. Provider prompts and sandbox controls therefore
remain in force unless an operator deliberately changes local configuration.

## Project harness API

Initialize a repository and open it as a scoped harness:

```powershell
herald init . --name my-app
herald register .
```

```python
from herald import Harness

app = Harness.open(".")
answer = app.code("Implement the next task")
health = app.tool("project_health")
backend = app.for_part("backend")
```

`Harness.ask()` is agentic by default. Herald gives the selected model only
the tools bound to that project part, validates every requested tool call at
the router, executes it, and returns the structured result to the model. The
same bounded tool loop works with CLI, API, local, and browser-session models.
The interactive equivalent is:

```powershell
herald agent --project my-app --part frontend
```

### Repeated decisions with one memory

For ordinary scripts, `create=True` generates the minimal `router.yaml` on
the first run. A named agent pins one logical model and keeps its user-visible
exchanges in an encrypted router-side memory cache:

```python
from herald import Harness

# The reviewed information comes from this list.
applications = [
    "Application 1: all required fields and documents are present",
    "Application 2: the required age field is missing",
]

app = Harness.open(".", create=True, name="application-review")
reviewer = app.agent(
    "Reviewer",
    memory="application-review-2026",
    instructions="Apply the same eligibility rules to every application.",
)

for application in applications:
    decision = reviewer.decide(
        question="What should happen to this application?",
        options=["approve", "reject", "needs-review"],
        context=application,
    )
    print(decision.choice, decision.reason, decision.confidence)
```

The calls are sequential: the second decision receives the same permanent
instructions plus the first input and response. Herald retains eight recent
exchanges in detail and compacts older exchanges into a bounded decision
ledger. It stores prompts and responses encrypted, never model-private hidden
reasoning. Reopening the same project, agent name, and memory name resumes the
cache; `reviewer.reset()` clears it. See `examples/review-loop.py` for a
complete runnable script.

Stateful calls use unique request IDs, so a repeated completed request is
returned from encrypted memory instead of becoming a duplicate turn. A
session rejects overlapping calls to protect sequential semantics. Retention
is configurable when the agent is opened:

```python
reviewer = app.agent(
    "Reviewer",
    recent_turns=12,
    ledger_entries=100,
    retention_days=90,
)
```

For dependent work, keep the same memory:

```python
results = reviewer.review_each(applications)
```

For independent work, Herald can safely run calls concurrently and retain
input ordering:

```python
results = app.map(applications, task="Classify this item", parallel=4)
```

`reviewer.decide(..., confidence_threshold=0.75)` marks low-confidence
results with `requires_human_review=True`. `result_type=MyPydanticModel` and
`reviewer.structured(prompt, MyPydanticModel)` validate application-specific
schemas, with one automatic JSON-repair attempt. Async forms include `aask`,
`areview`, `areview_each`, `adecide`, and `astructured`. `stream`/`astream` provide a stable
chunk iterator; current adapters buffer the provider response before yielding
chunks until native token streaming is available across the router.

Manage encrypted memories without exposing their content in normal listings:

```powershell
herald memory list
herald memory inspect <id-or-prefix>
herald memory reset <id-or-prefix>
herald memory export <id-or-prefix> memory.json
herald memory import <id-or-prefix> memory.json
herald memory delete <id-or-prefix>
```

Exports are deliberately decrypted JSON and may contain private inputs. The
Account Console provides list/reset/delete controls while keeping contents
hidden. A matching TypeScript client lives in `sdk/typescript`; Python 0.2's
supported compatibility surface is documented in `docs/PUBLIC_API.md`.

`router.yaml` separates tool installations from part bindings. Project tools
are stored under `.herald/tools`, are invisible to other projects, and may have
multiple versions installed at once. Global installations are visible to
unscoped calls, but scoped agents must still bind them explicitly.

```yaml
project: my-app
tools:
  - name: browser-v1
    package: playwright-mcp
    version: "1"
    transport: stdio
    command: [node, .herald/tools/browser-v1/server.js]
  - name: browser-v2
    package: playwright-mcp
    version: "2"
    transport: stdio
    command: [node, .herald/tools/browser-v2/server.js]
parts:
  frontend:
    tools: [{use: browser-v1, as: browser}]
  backend:
    tools: [{use: browser-v2, as: browser}]
```

Open-source MCP packages can be installed into an isolated project directory:

```powershell
herald tool install npm:@vendor/mcp-server --name server-v1 --version 1.2.0 `
  --binary mcp-server --project my-app --part frontend --as service
```

## Project policies

Herald is MIT licensed. Security reports, contributions, and support requests
follow [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md), and
[SUPPORT.md](SUPPORT.md). Bundled shell dependencies are credited in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
