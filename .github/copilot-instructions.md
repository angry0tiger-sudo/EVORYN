# EVORYN repository instructions

Follow the repository-root `AGENTS.md` as the governing engineering contract.

- Use Architecture First / Implementation Second / Evidence Always.
- Prefer Python where appropriate for research and backend components, but do not select frameworks before the Phase 0 architecture and dependency review.
- Keep the initial architecture a modular monolith with explicit interfaces.
- Keep exchange adapters, data providers, strategy logic, learning, risk controls, execution, and monitoring separated.
- No live trading in development or CI. Never add credentials or secrets to the repository.
- Verify current official Nobitex API documentation before relying on endpoint, order, margin, rate-limit, or authentication behavior.
- Add tests for every meaningful behavior change. Report tests not run instead of implying success.
- Do not add a dependency without documenting purpose, license, maintenance status, and alternatives.
- Do not make broad rewrites, merge automatically, deploy, or weaken risk controls.
- In final reports, list changed paths, evidence, unresolved assumptions, and next safe step.
