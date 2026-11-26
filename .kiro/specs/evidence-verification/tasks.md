# Tasks: Evidence-First Verification

- [x] 1. Manifest digest / seal / validate / approved — `evidence.py` _(R4)_
- [x] 2. Pin base SHA and compute net diff for code stages — `archive.py` _(R1)_
- [x] 3. Run test `argv` directly and archive output — `execution.py`, `archive.py`; tests: `test_failure_evidence.py` _(R2)_
- [x] 4. Structured review verdict and coverage policy — `review_policy.py`; tests: `test_review_coverage_policy.py` _(R3)_
- [x] 5. Bounded rework feedback loop — `review_policy.py`; tests: `test_review_feedback.py`, `test_review_regressions.py` _(R3)_
- [x] 6. Bind approvals to the approved manifest — `evidence.approved`, `engine.approve` _(R4)_
- [x] 7. Workspace write guards and violation records — `guards.py` _(R5)_
