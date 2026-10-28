# Decisions: one interface, two routes

Small choices go through `ultimate_assistant/decision_interface.py`. Each
`DecisionPoint` declares its typed questions once, an interpretation, and an
ordinary LLM and/or deterministic rule alternative. Questions support `noul`
(yes/no probability), `choice` and `score`.

The `decision_model` route is a mixed route, not a replacement: a normal model is
always required, and does everything a decision model shouldn't (conversation,
writing, extracting details, unsure cases). A decision model is added only at the
points in `DECISION_MODEL_POINTS`: observable yes/no questions, asked often, that
measured at least 90% correct and no slower than the normal model. Every other
point keeps its normal model or rule on both routes until it is measured and passes.

Decision models are for backend decisions that don't need a language model, not
for reproducing what one does. Modules wired together can trigger pre-programmed
responses, tools and routing so fewer model calls are needed, but the assistant is
never meant to run on decision models alone.

A decision model only answers yes/no (`noul`) questions. Each question is its own
decision; a point's questions share one request because they read the same input
(measured: the same answers as asking each alone, and faster). A point with any
other kind of question isn't a decision-model problem, so its alternative answers
it (fallback reason `not_yes_no`). Every question shares one bar,
`DEFAULT_CONFIDENCE` (0.7): a probability between 30% and 70% is unsure.

A choice among outcomes is a decision `Module`: one observable yes/no per outcome
(about the input only, never about what the assistant knows or can do). It is
settled in code when exactly one is a confident yes and the rest confident noes,
or all are confident noes (the module's `none` outcome). Anything else goes to the
alternative, which gets `Module.hints` (the questions that leaned yes). A branch's
next layer is another module, called in plain code.

Choose the route in the app's **Quick decisions** panel, including during setup:

| Setting | Meaning |
| --- | --- |
| `decision_route` | `llm` (default: the normal model and rules only) or `decision_model` (mixed: the normal model plus a decision model at the points in `DECISION_MODEL_POINTS`) |
| `decision_provider` | Optional user-assigned provider name from the connected Router |
| `decision_model` | Optional model override for that provider |

The same settings live in the user `settings.json`.
`ULTIMATE_ASSISTANT_DECISION_ROUTE` overrides the route for a process.
`ULTIMATE_ASSISTANT_DECISION_LLM_MODEL` selects a registered model/pool for
ordinary small LLM calls; otherwise they use Herald's `fast` capability.
The app suggests a local model or Jev without requiring either. Provider name
suggestions come from the connected Router's enabled inventory.

On `llm`, no decision provider is called. On `decision_model`, unavailable,
missing, malformed, non-finite, or uncertain answers fall back for that decision.
A probability of zero is a confident negative answer. Confident needs-tools
answers can go in either direction; broad words such as “wallpaper” do not
force another routing call after a confident negative.

| Point | Typed form | Ordinary route / fallback |
| --- | --- | --- |
| Agent and needs-tools routing | routing `Module`: noul per specialist (observable); all no is the companion, quick lane | work rules and agent-routing LLM, given the module's hints |
| Conversation shift | noul continues, noul per earlier conversation; all no is new | conversation-shift LLM |
| Skill match | noul per close skill; all no is none | matching LLM |
| Commitment gate | noul | embedding/lexical similarity rule |
| Module need | noul | module confirmation LLM |
| Leisure / importance | noul | LLM classification, then executable/structured-item rules |
| Model lane | noul heavy work | agent lane rule; conversational turns keep their fast path |
| Acknowledgement | none (four options, not a yes/no) | deterministic acknowledgement from the message's shape |
| Voice goodbye | noul for short utterances, on the main backend | farewell phrase rule |
| Toolsets | noul per connected server | LLM shortlist, then the complete scoped inventory |
| Known-option tool | none (many options, not a yes/no) | LLM choice, then no match |

Toolsets select a prompt shortlist within the current part's scope. They do not
change persistent scope or remove discovery access. `choose_known_option` is an
MCP tool in `assistant_skills`: it chooses among real theme, folder, window, app
or other names supplied by a caller, and never applies a change itself.
[Optional local audio turn detectors](../../docs/VOICE_TURN_DETECTION.md) integrate
with Linux desktop capture separately from the decision route setting.

Desktop goodbye checks call the authenticated backend endpoint
`POST /api/voice/turn-end`, so the main assistant's selected route governs them.
An older or unreachable backend retains the local phrase rule without making a
separate device-local decision-model request.

## SDK and accounting

UA calls `herald.client.RouterClient` for new Router interactions. Its
`decide(state, questions, provider=..., model=..., decision_name=...)` method
returns answers plus provider, model, latency and usage. An older SDK's
original two-argument default-backend call remains supported; explicit provider
selection and complete usage accounting require the updated SDK/Router pair.
UA never silently discards a selected provider to use another.

Each attempt writes metadata to `<user data>/decisions.jsonl`: decision name,
route, provider, model, latency, tokens, cost, status and fallback reason. It
contains no prompt, message or free-text answer. The app summarizes recent
records, showing how many costs are known. Rules report zero tokens/API cost;
unknown LLM/provider usage remains null.

Herald records its own provider attempts. UA reports ordinary LLM/rule attempts
through `RouterClient.record_decision`, translating the local log to the Router's
usage schema. Provider attempts are not reported twice. LLM response metadata
survives the SDK call; measured tokens/cost are retained when supplied, including
when answer parsing fails. Full work/skill execution costs remain in ordinary
Router usage rather than the classifier's decision totals.

## Verification boundaries

Tests cover both routes, uncertainty/failure fallback, actual SDK/API integration,
provider/model selection, usage accounting without duplication, optional voice
contracts, and inventory choices. Provider protocols have substitute-inference
contract tests; Jev was also probed live. Other hosted providers need the user's
own keys for live smoke tests. Local model licensing and exposing a device's
Ollama listener are separate configuration choices; this change does neither.
