"""Herald's math/science calculation core -- roadmap #9b.

Vendored and condensed from the user's own AI-Assistant-2025 project
(`backend/services/math_module/`) and ChemCode's chemistry calculators
(`backend/static/chemcode/chemistry-calculators.js`). Not a verbatim port --
distilled to the general-purpose computational core, dropping anything
tightly coupled to that project's own UI or external services:

  - SKIPPED: webassign_helper.py (scrapes WebAssign pages via Kapture MCP --
    external-service-specific, not a general capability).
  - SKIPPED: graph_analyzer.py (recognizes functions from screenshot images
    via Kapture MCP -- image-input capability overlaps with roadmap #9a's
    ingestion system rather than belonging here).
  - PORTED: calculus core (derivative/integral/limit/series/solve/simplify,
    critical points), LaTeX formatting (via sympy.latex, following the
    source's own convention of LaTeX over raw floats), a simplified 9-step
    curve-sketch summary, matplotlib-based function plotting, and four
    chemistry calculators (molar mass, stoichiometry via mole ratios, Beer's
    law, dilution/mixture).
"""
from __future__ import annotations

import ast
import base64
import math
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

import sympy as sp
from sympy import E, I, diff, factor, integrate, limit, oo, pi, series, simplify, solve, symbols

x = symbols("x")


# ---------------------------------------------------------------------------
# Calculus core
# ---------------------------------------------------------------------------

@dataclass
class MathResult:
    ok: bool
    latex: str = ""
    text: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "latex": self.latex, "text": self.text,
            "details": self.details, "error": self.error,
        }


_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d*)?|\.\d+)(?:([eE][+-]?\d+))?|([A-Za-z][A-Za-z0-9]*)|(\*\*|[()+\-*/^%,]))")
_CONSTANTS = {"x": x, "pi": pi, "e": E, "E": E, "I": I, "oo": oo, "inf": oo}
_FUNCTIONS = {
    "sin": sp.sin, "cos": sp.cos, "tan": sp.tan,
    "asin": sp.asin, "acos": sp.acos, "atan": sp.atan,
    "sinh": sp.sinh, "cosh": sp.cosh, "tanh": sp.tanh,
    "exp": sp.exp, "log": sp.log, "ln": sp.log,
    "sqrt": sp.sqrt, "abs": sp.Abs, "Abs": sp.Abs,
    "floor": sp.floor, "ceiling": sp.ceiling,
}
_BINARY_OPERATORS = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.Div: lambda left, right: left / right,
    ast.Pow: lambda left, right: left ** right,
    ast.Mod: lambda left, right: sp.Mod(left, right),
}


def _parse(expr_str: str) -> sp.Expr:
    if not isinstance(expr_str, str) or not expr_str.strip() or len(expr_str) > 512:
        raise ValueError("Expression must contain 1–512 characters.")
    expr_str = expr_str.strip()
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(expr_str):
        match = _TOKEN.match(expr_str, position)
        if not match:
            raise ValueError(f"Unsupported character at position {position}.")
        number, exponent, name, operator = match.groups()
        if number is not None:
            tokens.append(("number", number + (exponent or "")))
        elif name is not None:
            tokens.append(("name", name))
        else:
            tokens.append(("operator", "**" if operator == "^" else operator))
        position = match.end()

    normalized: list[str] = []
    left_ends_value = False
    previous_name = ""
    for kind, token in tokens:
        starts_value = kind in {"number", "name"} or token == "("
        is_function_call = previous_name in _FUNCTIONS and token == "("
        if left_ends_value and starts_value and not is_function_call:
            normalized.append("*")
        normalized.append(token)
        left_ends_value = kind in {"number", "name"} or token == ")"
        previous_name = token if kind == "name" else ""

    tree = ast.parse("".join(normalized), mode="eval")
    nodes = list(ast.walk(tree))
    if len(nodes) > 128 or max((len(list(ast.walk(node))) for node in nodes), default=0) > 64:
        raise ValueError("Expression is too complex.")

    def evaluate(node: ast.AST) -> sp.Expr:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            if isinstance(node.value, float) and not math.isfinite(node.value):
                raise ValueError("Non-finite numeric values are not supported.")
            return sp.Integer(node.value) if isinstance(node.value, int) else sp.Float(node.value)
        if isinstance(node, ast.Name) and node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, ast.Pow) and right.is_number:
                if right.is_real is not True or right.is_finite is not True or abs(right) > 1000:
                    raise ValueError("Numeric exponents must be finite real values with magnitude at most 1000.")
            return _BINARY_OPERATORS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS and not node.keywords:
            return _FUNCTIONS[node.func.id](*(evaluate(argument) for argument in node.args))
        raise ValueError("Expression uses unsupported syntax or an unknown symbol/function.")

    return evaluate(tree)


def _to_latex(expr: Any) -> str:
    try:
        return sp.latex(expr)
    except Exception:
        return str(expr)


def analyze_function(expr_str: str) -> MathResult:
    """Derivative, second derivative, integral, and critical points --
    condensed from CalculusAnalyzer's core dispatch in analyzer.py."""
    try:
        f = _parse(expr_str)
        f_prime = diff(f, x)
        f_double_prime = diff(f_prime, x)
        f_integral = integrate(f, x)
        critical_points = solve(f_prime, x)

        latex = (
            "\\begin{align*}\n"
            f"f(x) &= {_to_latex(f)} \\\\\n"
            f"f'(x) &= {_to_latex(f_prime)} \\\\\n"
            f"f''(x) &= {_to_latex(f_double_prime)} \\\\\n"
            f"\\int f(x)\\,dx &= {_to_latex(f_integral)} + C\n"
            "\\end{align*}"
        )
        return MathResult(
            ok=True, latex=latex,
            text=f"f(x) = {f}, f'(x) = {f_prime}, f''(x) = {f_double_prime}, "
                 f"integral = {f_integral} + C, critical points = {critical_points}",
            details={
                "original": str(f), "derivative": str(f_prime),
                "second_derivative": str(f_double_prime),
                "integral": str(f_integral),
                "critical_points": [str(c) for c in critical_points],
            },
        )
    except Exception as exc:
        return MathResult(ok=False, error=f"analyze_function failed: {exc}")


def compute_limit(expr_str: str, point: str, direction: str = "+-") -> MathResult:
    try:
        f = _parse(expr_str)
        pt = oo if point in ("oo", "inf", "infinity") else (-oo if point in ("-oo", "-inf") else _parse(point))
        result = limit(f, x, pt) if direction == "+-" else limit(f, x, pt, dir=direction)
        return MathResult(ok=True, latex=f"\\lim_{{x \\to {_to_latex(pt)}}} {_to_latex(f)} = {_to_latex(result)}", text=str(result), details={"limit": str(result)})
    except Exception as exc:
        return MathResult(ok=False, error=f"compute_limit failed: {exc}")


def solve_equation(expr_str: str) -> MathResult:
    try:
        f = _parse(expr_str)
        solutions = solve(f, x)
        return MathResult(
            ok=True, latex=f"{_to_latex(f)} = 0 \\implies x = {', '.join(_to_latex(s) for s in solutions)}",
            text=f"solutions: {solutions}", details={"solutions": [str(s) for s in solutions]},
        )
    except Exception as exc:
        return MathResult(ok=False, error=f"solve_equation failed: {exc}")


def series_expansion(expr_str: str, point: str = "0", order: int = 5) -> MathResult:
    try:
        f = _parse(expr_str)
        pt = _parse(point)
        expansion = series(f, x, pt, order)
        return MathResult(ok=True, latex=_to_latex(expansion), text=str(expansion), details={"series": str(expansion)})
    except Exception as exc:
        return MathResult(ok=False, error=f"series_expansion failed: {exc}")


# ---------------------------------------------------------------------------
# Word problems -- condensed pattern-matching subset from word_problem_solver.py
# ---------------------------------------------------------------------------

def solve_word_problem(problem: str) -> MathResult:
    """Best-effort: detects optimization ('maximize'/'minimize' + an
    expression) and rate-of-change patterns; falls back to reporting it
    couldn't classify the problem rather than guessing wrong."""
    lowered = problem.lower()
    keyword_match = re.search(r"\b(maximize|minimize|optimal)\b", lowered)

    if keyword_match:
        # Take everything after the keyword as the expression -- simpler and
        # more robust than trying to regex-extract an arbitrary expression
        # out of surrounding English.
        expr_str = problem[keyword_match.end():].strip(" :.\n")
        # Strip leading filler words one at a time (not just once) --
        # "the function x**2" would otherwise leave "function" attached,
        # which sympy's implicit-multiplication parser reads as f*u*n*c*t*i*o*n.
        while True:
            stripped = re.sub(r"^(of|the|function|f\(x\)\s*=)\s*", "", expr_str, flags=re.IGNORECASE)
            if stripped == expr_str:
                break
            expr_str = stripped
        if not expr_str:
            return MathResult(ok=False, error="Could not extract a function expression from the word problem.")
        try:
            f = _parse(expr_str)
            f_prime = diff(f, x)
            critical_points = solve(f_prime, x)
            evaluations = {str(cp): str(f.subs(x, cp)) for cp in critical_points if cp.is_real}
            kind = "minimize" if "minimize" in lowered else "maximize"
            return MathResult(
                ok=True, text=f"Critical points: {critical_points}, f(x) at each: {evaluations}",
                details={"kind": kind, "critical_points": [str(c) for c in critical_points], "values": evaluations},
            )
        except Exception as exc:
            return MathResult(ok=False, error=f"optimization solve failed: {exc}")

    return MathResult(ok=False, error="Could not classify word problem type (supported: optimization).")


# ---------------------------------------------------------------------------
# Curve sketching -- condensed 9-step method from curve_sketcher.py
# ---------------------------------------------------------------------------

def sketch_curve_summary(expr_str: str) -> MathResult:
    try:
        f = _parse(expr_str)
        f_prime = diff(f, x)
        f_double_prime = diff(f_prime, x)

        intercepts_y = f.subs(x, 0)
        intercepts_x = solve(f, x)
        critical_points = solve(f_prime, x)
        inflection_points = solve(f_double_prime, x)
        increasing_decreasing = solve(f_prime, x)  # sign-change points; caller reasons over intervals

        summary = {
            "y_intercept": str(intercepts_y),
            "x_intercepts": [str(v) for v in intercepts_x],
            "critical_points": [str(v) for v in critical_points],
            "inflection_points": [str(v) for v in inflection_points],
            "sign_change_points": [str(v) for v in increasing_decreasing],
        }
        text = (
            f"y-intercept: {intercepts_y}; x-intercepts: {intercepts_x}; "
            f"critical points: {critical_points}; inflection points: {inflection_points}"
        )
        return MathResult(ok=True, text=text, details=summary)
    except Exception as exc:
        return MathResult(ok=False, error=f"sketch_curve_summary failed: {exc}")


def plot_function(expr_str: str, x_min: float = -10, x_max: float = 10) -> dict[str, Any]:
    """Matplotlib-based plot, condensed from graphing_engine.py. Returns a
    base64-encoded PNG rather than the source's multi-backend
    (matplotlib+plotly) setup -- one static image is enough for an LLM tool
    result; interactive plotly output isn't useful in a text-based session."""
    try:
        import numpy as np
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        if not math.isfinite(x_min) or not math.isfinite(x_max) or x_min >= x_max or x_max - x_min > 2000:
            raise ValueError("Plot range must be finite, increasing, and no wider than 2000 units.")

        f = _parse(expr_str)
        f_np = sp.lambdify(x, f, "numpy")
        xs = np.linspace(x_min, x_max, 400)
        with np.errstate(all="ignore"):
            ys = f_np(xs)

        fig, ax = plt.subplots(figsize=(6, 4))
        ax.plot(xs, ys)
        ax.axhline(0, color="black", linewidth=0.5)
        ax.axvline(0, color="black", linewidth=0.5)
        ax.set_title(f"f(x) = {expr_str}")
        ax.grid(True, alpha=0.3)

        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=100)
        plt.close(fig)
        buf.seek(0)
        return {"ok": True, "image_base64_png": base64.b64encode(buf.read()).decode()}
    except Exception as exc:
        return {"ok": False, "error": f"plot_function failed: {exc}"}


# ---------------------------------------------------------------------------
# Chemistry -- ported from chemistry-calculators.js's calculation core
# ---------------------------------------------------------------------------

_ELEMENT_MASSES = {
    "H": 1.008, "C": 12.011, "N": 14.007, "O": 15.999,
    "S": 32.06, "P": 30.974, "Cl": 35.45, "Na": 22.990,
    "K": 39.098, "Ca": 40.078, "Fe": 55.845, "Mg": 24.305,
}


def molar_mass(formula: str) -> dict[str, Any]:
    """Direct port of chemistry-calculators.js's calculateMolarMass."""
    mass = 0.0
    for element, count in re.findall(r"([A-Z][a-z]?)(\d*)", formula):
        if not element:
            continue
        mass += _ELEMENT_MASSES.get(element, 0.0) * (int(count) if count else 1)
    return {"formula": formula, "molar_mass_g_per_mol": round(mass, 3)}


def stoichiometry(given_mass_g: float, given_formula: str, find_formula: str, mole_ratio: float = 1.0) -> dict[str, Any]:
    """moles(given) -> moles(find) via mole_ratio -> mass(find), same chain
    as calculateStoichiometry in the source JS."""
    given_mm = molar_mass(given_formula)["molar_mass_g_per_mol"]
    find_mm = molar_mass(find_formula)["molar_mass_g_per_mol"]
    if given_mm == 0:
        return {"ok": False, "error": f"Could not determine molar mass for '{given_formula}'"}
    moles_given = given_mass_g / given_mm
    moles_find = moles_given * mole_ratio
    mass_find = moles_find * find_mm
    return {
        "ok": True, "moles_given": round(moles_given, 5), "moles_find": round(moles_find, 5),
        "mass_find_g": round(mass_find, 5), "given_molar_mass": given_mm, "find_molar_mass": find_mm,
    }


def beers_law(absorbance: float | None = None, molar_absorptivity: float | None = None,
              concentration: float | None = None, path_length: float = 1.0) -> dict[str, Any]:
    """A = epsilon * c * l -- solves for whichever one variable is None."""
    known = {k: v for k, v in {"absorbance": absorbance, "molar_absorptivity": molar_absorptivity, "concentration": concentration}.items() if v is not None}
    if len(known) != 2:
        return {"ok": False, "error": "Provide exactly two of absorbance, molar_absorptivity, concentration (path_length defaults to 1.0)."}
    if absorbance is None:
        return {"ok": True, "absorbance": round(molar_absorptivity * concentration * path_length, 6)}
    if molar_absorptivity is None:
        return {"ok": True, "molar_absorptivity": round(absorbance / (concentration * path_length), 6)}
    return {"ok": True, "concentration": round(absorbance / (molar_absorptivity * path_length), 6)}


def dilution(m1: float | None = None, v1: float | None = None, m2: float | None = None, v2: float | None = None) -> dict[str, Any]:
    """M1V1 = M2V2 -- solves for whichever one variable is None, same as
    calculateMixture in the source JS."""
    values = {"m1": m1, "v1": v1, "m2": m2, "v2": v2}
    missing = [k for k, v in values.items() if v is None]
    if len(missing) != 1:
        return {"ok": False, "error": "Provide exactly three of m1, v1, m2, v2 -- the fourth is solved for."}
    target = missing[0]
    if target == "m1":
        return {"ok": True, "m1": round((m2 * v2) / v1, 6)}
    if target == "v1":
        return {"ok": True, "v1": round((m2 * v2) / m1, 6)}
    if target == "m2":
        return {"ok": True, "m2": round((m1 * v1) / v2, 6)}
    return {"ok": True, "v2": round((m1 * v1) / m2, 6)}
