# EVORYN Agent Setup

## What is present in this branch

This branch introduces repository-level agent instructions and specialized profiles:

- `AGENTS.md`: baseline contract for any coding agent.
- `.github/copilot-instructions.md`: repository-wide instructions for supported Copilot tooling.
- `.github/agents/evoryn-architect.agent.md`: architecture and decision records.
- `.github/agents/evoryn-builder.agent.md`: scoped implementation.
- `.github/agents/evoryn-risk-reviewer.agent.md`: independent safety/security review.
- `.github/agents/evoryn-test-engineer.agent.md`: tests and evidence.

These are versioned agent profiles, not proof that a model runtime or GitHub App has been installed or executed. Supported IDE/CLI hosts can discover compatible profiles; availability depends on the host and its account/feature entitlement.

## Free software versus free inference

Open-source licensing does not make model inference, hosted runners, or external services automatically free. Before enabling an automated hosted agent, verify:
1. The agent software's current license.
2. The selected model's local/hosted cost and license.
3. Required GitHub App permissions and workflow permissions.
4. Whether any required secret or paid service is involved.
5. Where code and repository content are sent or executed.

## Candidate runtimes

### OpenHands
Repository: https://github.com/OpenHands/OpenHands

Open-source software candidate for autonomous software engineering. A self-hosted setup can use a compatible local model, but it still needs runtime resources and explicit configuration. A GitHub-hosted workflow is not considered enabled merely by adding a profile; model credentials, runner setup, and least-privilege permissions must be reviewed first.

### Aider
Repository: https://github.com/Aider-AI/aider

Open-source AI pair-programming tool that can work with local or hosted models. It is primarily a developer-side tool, not a GitHub-hosted autonomous agent by itself. Use an isolated environment and a dedicated branch.

### Freqtrade / FreqAI
Repository: https://github.com/freqtrade/freqtrade

Candidate for research and backtesting evaluation only. It is not a general software engineering agent and must not become a core EVORYN dependency before a compatibility, licensing, and architecture review. Do not assume Nobitex Margin support.

## Local runtime bootstrap

See [Local Runtime Bootstrap](LOCAL_RUNTIME_BOOTSTRAP.md) for the Windows + Ollama setup sequence, isolation requirements, least-privilege GitHub access policy, and evidence required before an agent can be called active. The actual development host was offline at the latest check, so installation and runtime validation remain pending.

## Activation checklist

- [ ] Review this branch and merge only after human review.
- [ ] Choose the runtime and model explicitly.
- [ ] Confirm model license, inference cost, and privacy boundary.
- [ ] Use a sandbox/container and least-privilege GitHub access.
- [ ] Do not provide exchange API keys or live-trading permissions to any coding agent.
- [ ] Require pull requests and independent review; do not auto-merge.
- [ ] Run tests and record evidence before accepting agent-authored changes.

## Initial policy

No autonomous agent workflow is enabled by this change. This is intentional: adding a workflow that silently depends on paid inference or broad write permissions would create the appearance of a free installation without a verified, safe runtime.
