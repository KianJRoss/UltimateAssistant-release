# Routing benchmark

`route_bench.py` runs the assistant's real pre-turn routing code on synthetic prompts and records, per case, the expected and actual route, who decided it (decision model, LLM fallback or keyword rule), latency, and each decider's answers. It never executes tools.

Results from this benchmark are published at
https://huggingface.co/datasets/Krioasns/system1-decision-routing-bench (also on Kaggle as `kianross/system1-decision-routing-bench`).

This tool is not part of the app bundle. `apps/` and `components/` still match the release downloads exactly.

## LLM-only run (just Ollama)

From the repository root, with Ollama serving a model:

```bash
cd apps/assistant
export LOCALAPPDATA=/tmp/ua-bench   # disposable; the path must contain "bench"
PYTHONPATH=$PWD uv run --no-project --with httpx --with-requirements requirements.txt --with https://github.com/KianJRoss/herald-release/releases/download/v0.4.2/herald_sdk-0.4.2-py3-none-any.whl \
  --with https://github.com/KianJRoss/herald-release/releases/download/v0.4.2/herald_router-0.4.2-py3-none-any.whl \
  python ../../tools/decisions/route_bench.py \
  --engines llm --llm-model qwen3.5:9b --native-llm-url http://127.0.0.1:11434 \
  --repeats 1 --groups agent agent_held_out agent_fresh agent_adversarial surmise \
  --baseline-name qwen3.5-9b --output /tmp/ua-bench/qwen3.5-9b.jsonl
```

The last two `--with` lines install the Herald SDK and Router 0.4.2 wheels from https://github.com/KianJRoss/herald-release (it isn't on PyPI). On 0.4.4 this reproduces the published `qwen3.5-9b` row: 37/40 routing, 9/10 adversarial, 12/12 surmise.

A `.summary.json` with per-group accuracy, latency and fallback counts is written next to the output.

## Decision-model runs

Engines other than `llm` (for example `tev1`, `jev`) are resolved through a Herald Router configured with those decision providers. Use a router that contains only benchmark providers and one non-agentic LLM backend, so nothing else is called.

Note: the published decider results come from the next version of the routing code, which adds "dominant single yes" settling (one option ≥0.85, all others <0.50, gap ≥0.35). On 0.4.4, decision models settle fewer turns on their own and fall back to the LLM more often. Accuracy is unaffected.

## Reading the results

Results are diagnostic, not a quality eval: the prompts are synthetic and specific to this assistant's six agents. Each output row is one case; `correct` compares the full output with the expected one, so read `value` against `expected` to see which part was wrong.
