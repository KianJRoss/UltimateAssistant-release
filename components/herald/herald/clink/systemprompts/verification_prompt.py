"""Shared verification-discipline fragment appended to clink workflows that
make claims about correctness, completion, or a fix/vulnerability/test being
real -- generate_code, refactor, testgen, codereview, debug, secaudit, and
precommit. Added after Herald was found reporting a code change as "verified"
and "delivered" when it had never actually compiled, based on a stale log
from an unrelated earlier run. The general coder persona (steering.py) got
this rule first; this is the same rule for the specialized workflows, which
share the same fabrication risk but don't share that persona.
"""

VERIFICATION_DISCIPLINE = """

## Verification Discipline

- Never invent a type, function signature, header, or API you have not confirmed exists in the
  actual codebase you were given. If you did not read its real declaration, say you are inferring
  it, not that it is confirmed.
- A claim that something is fixed, tested, vulnerable, or working is a claim about the CURRENT
  state of the code you were shown -- not about what a similar-looking change usually does. Base
  it only on what you actually read in this request, never on a prior run's output, a cached
  summary, or a log file whose timestamp you have not checked against the current change.
- If you were not given a build/test/compile result for this exact change, say so plainly instead
  of asserting the change works. "This should compile" and "this compiles" are different claims --
  only make the second one when you have real evidence for it.
"""
