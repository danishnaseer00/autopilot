# paas/console — Worker C (SPEC-36 contract → SPEC-37 implementation)

React + Vite (ADR-5).

1. **SPEC-36 is read-only for you:** the API contract is written first and
   merged before this app starts. Invented endpoints are how the console
   silently drifts (SPEC-38 R-5 tester catches it; don't be the reason it does).
2. Structure: API client generated against SPEC-36, no ad-hoc `fetch`;
   state in TanStack Query + Zustand per product need; forms with validation
   up front, not after submit.
3. Build the boring states well: loading, empty, error, 403. The console must
   survive a platform error without lying to the user.
4. Security: XSS surface discipline, token storage per SPEC-05's decision,
   no secrets ever in localStorage (SPEC-18 redaction applies to you too).
5. Tests: component tests + the console contract test suite in
   `../tests/contract/` must pass before your stop-report.
