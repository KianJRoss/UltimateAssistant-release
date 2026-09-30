"""Herald built-in math/science calculation tools -- an MCP server exposing
solve_math, sketch_graph, and chemistry_calc to any agentic session.

Vendored from the user's own AI-Assistant-2025 math_module and ChemCode's
chemistry calculators (see herald/router/math_tools.py's module docstring
for exactly what was ported vs skipped and why).

Separate process from herald-coding-tools/herald-ingest-tools since sympy/
matplotlib are only needed here.

Transport: stdio (launched as a subprocess by Herald's bootstrap).
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from herald.router.math_tools import (
    analyze_function, beers_law, compute_limit, dilution, molar_mass,
    plot_function, series_expansion, sketch_curve_summary, solve_equation,
    solve_word_problem, stoichiometry,
)

mcp = FastMCP("herald-math")


@mcp.tool()
def solve_math(
    expression_or_problem: str, mode: str = "auto", point: str = "0",
    direction: str = "+-", order: int = 5,
) -> str:
    """Solve a calculus/algebra expression or word problem symbolically.

    Args:
        expression_or_problem: A math expression in `x` (e.g. "x**2 - 3*x + 2")
            or a plain-English word problem (e.g. "maximize 100x - x**2").
        mode: One of "auto" (default -- analyzes derivative/integral/critical
            points, or solves a word problem if it reads like one), "limit",
            "solve" (solve expression = 0), or "series".
        point: Limit/series expansion point (default "0").
        direction: Limit direction: "+", "-", or "+-" (default).
        order: Series expansion order (default 5).

    Returns:
        JSON with ok, latex (LaTeX-formatted result, preferred for display
        over raw numbers), text, and details.
    """
    if mode == "limit":
        result = compute_limit(expression_or_problem, point, direction)
    elif mode == "solve":
        result = solve_equation(expression_or_problem)
    elif mode == "series":
        result = series_expansion(expression_or_problem, point, order)
    elif mode == "word_problem":
        result = solve_word_problem(expression_or_problem)
    else:
        result = analyze_function(expression_or_problem)
        if not result.ok:
            # auto mode: if it doesn't parse as an expression, try word-problem solving
            result = solve_word_problem(expression_or_problem)
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)


@mcp.tool()
def sketch_graph(function: str, x_min: float = -10, x_max: float = 10, summary_only: bool = False) -> str:
    """Sketch a function: intercepts/critical/inflection points plus,
    unless summary_only, a rendered plot image.

    Args:
        function: A function of x, e.g. "x**3 - 3*x".
        x_min, x_max: Plot range (ignored if summary_only=True).
        summary_only: If True, skip rendering and return just the analytical
            summary (faster, no image payload).

    Returns:
        JSON with the 9-step-method summary and, unless summary_only, an
        "image_base64_png" field with a rendered plot.
    """
    summary = sketch_curve_summary(function)
    payload = summary.to_dict()
    if not summary_only:
        plot = plot_function(function, x_min, x_max)
        payload["plot"] = plot
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool()
def chemistry_calc(
    calculation_type: str, formula: str = "", given_mass_g: float = 0,
    given_formula: str = "", find_formula: str = "", mole_ratio: float = 1.0,
    absorbance: float | None = None, molar_absorptivity: float | None = None,
    concentration: float | None = None, path_length: float = 1.0,
    m1: float | None = None, v1: float | None = None,
    m2: float | None = None, v2: float | None = None,
) -> str:
    """Run a chemistry calculation.

    Args:
        calculation_type: One of "molar_mass", "stoichiometry", "beers_law",
            "dilution".
        formula, given_mass_g, given_formula, find_formula, mole_ratio,
        absorbance, molar_absorptivity, concentration, path_length, m1, v1,
        m2, v2: Calculation-specific inputs. For dilution provide any three
            of m1, v1, m2, v2; the fourth is solved for.

    Returns:
        JSON with the calculation result.
    """
    if calculation_type == "molar_mass":
        result = molar_mass(formula)
    elif calculation_type == "stoichiometry":
        result = stoichiometry(
            given_mass_g, given_formula, find_formula, mole_ratio,
        )
    elif calculation_type == "beers_law":
        result = beers_law(
            absorbance, molar_absorptivity, concentration, path_length,
        )
    elif calculation_type == "dilution":
        result = dilution(m1, v1, m2, v2)
    else:
        result = {"ok": False, "error": f"Unknown calculation_type '{calculation_type}'. Supported: molar_mass, stoichiometry, beers_law, dilution."}
    return json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
