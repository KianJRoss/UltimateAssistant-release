"""Herald's declarative configuration and Python API for multi-agent flows.

Topology (``flow``) is intentionally separate from behavioral instructions
(``rules``).  Named agents may point at the same model while retaining wholly
different memory caches.
"""
from __future__ import annotations

import ast
import operator
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
TEMPLATE_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")

_BINARY_OPERATORS = {
    ast.Add: operator.add,
}
_COMPARISON_OPERATORS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.In: lambda left, right: left in right,
    ast.NotIn: lambda left, right: left not in right,
    ast.Is: operator.is_, ast.IsNot: operator.is_not,
}


def evaluate_condition(expression: str, context: dict[str, Any]) -> bool:
    """Evaluate the deliberately small, side-effect-free flow condition grammar.

    Supported forms are names, literals, boolean operators, comparisons,
    membership tests, ``not``, and string/list/tuple concatenation. Attribute
    access, calls, comprehensions, subscripting, and every other Python node are
    rejected instead of being executed.
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid flow condition: {exc.msg}") from exc

    def visit(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (str, int, float, bool, type(None))):
                return node.value
        elif isinstance(node, ast.Name):
            if node.id in context:
                return context[node.id]
            raise ValueError(f"unknown condition name '{node.id}'")
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = [visit(item) for item in node.elts]
            return tuple(values) if isinstance(node, ast.Tuple) else (set(values) if isinstance(node, ast.Set) else values)
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            values = [bool(visit(value)) for value in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not bool(visit(node.operand))
        elif isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
            left, right = visit(node.left), visit(node.right)
            if not isinstance(left, (str, list, tuple)) or not isinstance(right, type(left)):
                raise ValueError("condition '+' only supports matching strings or sequences")
            return _BINARY_OPERATORS[type(node.op)](left, right)
        elif isinstance(node, ast.Compare):
            left = visit(node.left)
            for operation, comparator in zip(node.ops, node.comparators):
                right = visit(comparator)
                function = _COMPARISON_OPERATORS.get(type(operation))
                if function is None:
                    raise ValueError("unsupported comparison operator")
                try:
                    matched = function(left, right)
                except (TypeError, ValueError) as exc:
                    raise ValueError("incompatible values in flow condition") from exc
                if not matched:
                    return False
                left = right
            return True
        raise ValueError(f"unsupported flow condition syntax: {type(node).__name__}")

    return bool(visit(tree))


@dataclass(frozen=True)
class AgentSpec:
    name: str
    model: str = "auto"
    memory: str = ""
    tools: bool = True


@dataclass(frozen=True)
class StageSpec:
    targets: tuple[str, ...]
    sources: tuple[str, ...] = ()
    message: str | None = None
    parallel: bool = True
    condition: str | None = None
    require_approval: bool = False


@dataclass(frozen=True)
class FlowSpec:
    name: str
    mode: str
    agents: dict[str, AgentSpec]
    stages: tuple[StageSpec, ...]
    rules: dict[str, str] = field(default_factory=dict)
    limits: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "FlowSpec":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FlowSpec":
        if not isinstance(data, dict):
            raise ValueError("flow document must be an object")
        name = str(data.get("name") or "flow")
        raw_agents = data.get("agents") or {}
        if not isinstance(raw_agents, dict) or not raw_agents:
            raise ValueError("agents must be a non-empty object")
        if len(raw_agents) > 64:
            raise ValueError("a flow may define at most 64 agents")
        agents: dict[str, AgentSpec] = {}
        for agent_name, raw in raw_agents.items():
            if not NAME_PATTERN.match(str(agent_name)):
                raise ValueError(f"invalid agent name '{agent_name}'")
            raw = {"model": raw} if isinstance(raw, str) else (raw or {})
            if not isinstance(raw, dict):
                raise ValueError(f"agent '{agent_name}' must be a model string or object")
            agents[str(agent_name)] = AgentSpec(
                name=str(agent_name), model=str(raw.get("model") or "auto"),
                memory=str(raw.get("memory") or agent_name), tools=bool(raw.get("tools", True)),
            )

        raw_stages = data.get("flow") or data.get("stages") or []
        if not isinstance(raw_stages, list) or not raw_stages:
            raise ValueError("flow must be a non-empty list")
        if len(raw_stages) > 128:
            raise ValueError("a flow may contain at most 128 stages")
        stages = tuple(_parse_stage(raw, agents) for raw in raw_stages)
        rules = data.get("rules") or {}
        if not isinstance(rules, dict):
            raise ValueError("rules must be an object separate from flow topology")
        unknown_rules = set(rules) - set(agents)
        if unknown_rules:
            raise ValueError(f"rules reference unknown agents: {', '.join(sorted(unknown_rules))}")
        limits = data.get("limits") or {}
        if not isinstance(limits, dict):
            raise ValueError("limits must be an object")
        return cls(
            name=name, mode=str(data.get("mode") or "efficiency"), agents=agents,
            stages=stages, rules={str(k): str(v) for k, v in rules.items()},
            limits={str(k): int(v) for k, v in limits.items()},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1, "name": self.name, "mode": self.mode,
            "agents": {
                name: {"model": agent.model, "memory": agent.memory, "tools": agent.tools}
                for name, agent in self.agents.items()
            },
            "flow": [
                {
                    "to": list(stage.targets), "from": list(stage.sources),
                    "message": stage.message, "parallel": stage.parallel,
                    "condition": stage.condition, "require_approval": stage.require_approval,
                }
                for stage in self.stages
            ],
            "rules": self.rules, "limits": self.limits,
        }


def _names(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ValueError("stage targets/sources must be a name or list of names")


def _parse_stage(raw: Any, agents: dict[str, AgentSpec]) -> StageSpec:
    condition: str | None = None
    require_approval: bool = False
    if isinstance(raw, (str, list)):
        targets, sources, message, parallel = _names(raw), (), None, True
    elif isinstance(raw, dict):
        targets = _names(raw.get("to"))
        sources = _names(raw.get("from"))
        message = None if raw.get("message") is None else str(raw["message"])
        parallel = bool(raw.get("parallel", True))
        condition = raw.get("if") or raw.get("condition") or raw.get("next_if")
        require_approval = bool(raw.get("require_approval", False))
    else:
        raise ValueError("each flow stage must be a name, list, or object")
    if not targets:
        raise ValueError("each flow stage needs at least one target")
    unknown = (set(targets) | set(sources)) - set(agents) - {"input", "output"}
    if unknown:
        raise ValueError(f"flow references unknown agents: {', '.join(sorted(unknown))}")
    if "output" in targets and len(targets) != 1:
        raise ValueError("output must be the only target in its stage")
    return StageSpec(
        targets=targets, sources=sources, message=message,
        parallel=parallel, condition=condition, require_approval=require_approval,
    )


@dataclass(frozen=True)
class AgentRef:
    name: str


class Flow:
    """Small Python builder that serializes to the same format as flow YAML."""

    def __init__(self, name: str = "flow", *, mode: str = "efficiency") -> None:
        self.name, self.mode = name, mode
        self._agents: dict[str, dict[str, Any]] = {}
        self._flow: list[dict[str, Any]] = []
        self._rules: dict[str, str] = {}
        self._limits: dict[str, int] = {}

    def agent(self, name: str, model: str = "auto", *, memory: str | None = None, tools: bool = True) -> AgentRef:
        self._agents[name] = {"model": model, "memory": memory or name, "tools": tools}
        return AgentRef(name)

    def then(self, *agents: AgentRef | str, from_: list[AgentRef | str] | None = None,
             message: str | None = None, parallel: bool = True) -> "Flow":
        names = [agent.name if isinstance(agent, AgentRef) else str(agent) for agent in agents]
        sources = [agent.name if isinstance(agent, AgentRef) else str(agent) for agent in (from_ or [])]
        self._flow.append({"to": names, "from": sources, "message": message, "parallel": parallel})
        return self

    def rule(self, agent: AgentRef | str, instructions: str) -> "Flow":
        name = agent.name if isinstance(agent, AgentRef) else str(agent)
        self._rules[name] = instructions
        return self

    def limit(self, name: str, value: int) -> "Flow":
        self._limits[name] = value
        return self

    def spec(self) -> FlowSpec:
        return FlowSpec.from_dict({
            "name": self.name, "mode": self.mode, "agents": self._agents,
            "flow": self._flow, "rules": self._rules, "limits": self._limits,
        })

    def to_dict(self) -> dict[str, Any]:
        return self.spec().to_dict()


class FlowRunner:
    def __init__(self, call_agent: Callable[[AgentSpec, str, str], dict[str, Any]]) -> None:
        self.call_agent = call_agent

    def run(
        self, spec: FlowSpec, input_text: str, *, state: dict[str, Any] | None = None,
        checkpoint: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        state = state or {}
        outputs: dict[str, str] = dict(state.get("outputs") or {"input": input_text})
        previous: dict[str, str] = dict(state.get("previous") or {"input": input_text})
        memories: dict[str, list[tuple[str, str]]] = {
            name: [tuple(item) for item in items]
            for name, items in (state.get("memories") or {}).items()
        }
        trace: list[dict[str, Any]] = list(state.get("trace") or [])
        final = str(state.get("final") or input_text)
        next_stage = max(0, int(state.get("next_stage") or 0))

        for index, stage in enumerate(spec.stages[next_stage:], start=next_stage):
            # Check conditional stage execution if defined
            if stage.condition:
                eval_context = {"input": input_text, **outputs}
                try:
                    if not evaluate_condition(stage.condition, eval_context):
                        trace.append({"stage": index, "skipped": True, "condition": stage.condition})
                        continue
                except ValueError as exc:
                    raise ValueError(f"stage {index} condition is unsafe or invalid: {exc}") from exc

            if stage.require_approval:
                trace.append({"stage": index, "paused_for_approval": True})
                if checkpoint:
                    checkpoint(self._state(index, outputs, previous, memories, trace, final, status="pending_approval"))
                return {
                    "content": final, "outputs": outputs, "trace": trace, "mode": spec.mode,
                    "status": "pending_approval", "pending_stage": index,
                }

            if stage.targets == ("output",):
                final = self._message(stage, spec, input_text, outputs, previous)
                trace.append({"stage": index, "output": True, "sources": list(stage.sources)})
                if checkpoint:
                    checkpoint(self._state(index + 1, outputs, previous, memories, trace, final, status="completed"))
                break
            incoming = self._message(stage, spec, input_text, outputs, previous)

            def invoke(name: str) -> tuple[str, dict[str, Any]]:
                agent = spec.agents[name]
                memory = memories.setdefault(agent.memory, [])
                memory_text = "\n\n".join(
                    f"Earlier input: {old_input}\nEarlier response: {old_output}"
                    for old_input, old_output in memory[-4:]
                )
                prompt = ""
                if spec.rules.get(name):
                    prompt += f"[Rules for {name}]\n{spec.rules[name]}\n\n"
                if memory_text:
                    prompt += f"[Memory cache: {agent.memory}]\n{memory_text}\n\n"
                prompt += f"[Incoming flow data]\n{incoming}"
                result = self.call_agent(agent, prompt, spec.mode)
                content = str(result.get("content", ""))
                memory.append((incoming[-12000:], content[-12000:]))
                return name, {**result, "content": content}

            stage_results: dict[str, dict[str, Any]] = {}
            if stage.parallel and len(stage.targets) > 1:
                with ThreadPoolExecutor(max_workers=len(stage.targets)) as executor:
                    futures = [executor.submit(invoke, name) for name in stage.targets]
                    for future in as_completed(futures):
                        name, result = future.result()
                        stage_results[name] = result
            else:
                for name in stage.targets:
                    result_name, result = invoke(name)
                    stage_results[result_name] = result
            previous = {name: stage_results[name]["content"] for name in stage.targets}
            outputs.update(previous)
            final = "\n\n".join(f"[{name}]\n{value}" for name, value in previous.items())
            trace.append({
                "stage": index, "targets": list(stage.targets), "sources": list(stage.sources),
                "parallel": stage.parallel and len(stage.targets) > 1,
                "agents": {
                    name: {key: value for key, value in stage_results[name].items() if key != "content"}
                    for name in stage.targets
                },
            })
            if checkpoint:
                checkpoint(self._state(index + 1, outputs, previous, memories, trace, final))
        return {"content": final, "outputs": outputs, "trace": trace, "mode": spec.mode}

    @staticmethod
    def _state(next_stage: int, outputs: dict[str, str], previous: dict[str, str],
               memories: dict[str, list[tuple[str, str]]], trace: list[dict[str, Any]],
               final: str, status: str = "in_progress") -> dict[str, Any]:
        return {
            "next_stage": next_stage, "outputs": outputs, "previous": previous,
            "memories": memories, "trace": trace, "final": final, "status": status,
        }

    @staticmethod
    def _message(stage: StageSpec, spec: FlowSpec, input_text: str,
                 outputs: dict[str, str], previous: dict[str, str]) -> str:
        if stage.message is not None:
            values = {"input": input_text, **outputs}
            return TEMPLATE_PATTERN.sub(lambda match: values.get(match.group(1), ""), stage.message)
        selected = stage.sources or tuple(previous)
        values = outputs if stage.sources else previous
        return "\n\n".join(f"[{name}]\n{values.get(name, '')}" for name in selected)
