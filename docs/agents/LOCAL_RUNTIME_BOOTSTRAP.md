# Local Agent Runtime Bootstrap (Windows + Ollama)

## Status and gate

This document is a controlled bootstrap procedure, not evidence of an installed runtime. Do not mark the agent as active until the checks in the final section pass on the actual development host.

The currently registered Desktop Commander device was reported **offline** during the 2026-10-18 check. Therefore this runbook was added to the GitHub branch, but no local packages, Docker containers, models, or GitHub credentials have been installed or changed by this step.

## Chosen sequence

1. **Primary candidate: OpenHands**, running locally with a Docker sandbox and a compatible local model served by Ollama.
2. **Fallback / focused coding helper: Aider**, using Ollama and a dedicated Git worktree. Do not install both until the OpenHands feasibility check shows a concrete gap.
3. No GitHub Actions workflow will run an autonomous coding agent in this phase. This avoids hidden hosted-model costs, secret exposure, and unreviewed write permissions.

Official references:
- OpenHands local LLM guide: https://docs.all-hands.dev/openhands/usage/llms/local-llms
- OpenHands Docker sandbox: https://docs.all-hands.dev/openhands/usage/sandboxes/docker
- OpenHands local setup: https://docs.all-hands.dev/openhands/usage/run-openhands/local-setup
- Aider with Ollama: https://aider.chat/docs/llms/ollama.html
- GitHub Actions security: https://docs.github.com/en/actions/reference/security

Check the current official instructions before installation because commands, image tags, model support, and hardware requirements can change.

## Hard isolation requirements

- Use a dedicated Windows account or disposable VM where practical. At minimum, use a dedicated checkout/worktree for EVORYN and do not mount unrelated project folders.
- Do not mount the user's home directory, SSH directory, Git credential stores, cloud credential folders, exchange configuration, or any directory containing secrets into an agent container.
- Never provide exchange API keys, account secrets, withdrawal permissions, or live-trading credentials to an agent.
- Keep the repository free of secrets. Agent instructions and prompts are not a secret store.
- Keep the local Ollama API bound to loopback unless a reviewed network design requires otherwise. Do not expose it to a LAN or public interface by default.
- Bind the agent UI to loopback only. Do not expose port 3000 publicly.
- Docker socket access is highly privileged. OpenHands may require it for its Docker sandbox. Do not treat the Docker socket as a security boundary; the sandbox configuration must be reviewed and tested. Prefer a disposable VM or dedicated host with no sensitive credentials.
- Start with a read-only GitHub view or a local clone without authenticated write credentials. If later needed, use a dedicated GitHub App or fine-grained token scoped only to this repository and only to the minimum required permissions. Store credentials outside the repo and inject them only for the narrow operation that needs them.
- Do not grant repository administration, Actions write, secrets write, workflow write, or auto-merge permissions to the agent.
- Keep branch protection and human review. Agent-authored changes must be presented as a branch/PR; never auto-merge.
- Do not create an agent workflow using `pull_request_target` or expose repository secrets to pull-request code.

## Phase A: inspect before install

Run these in PowerShell on the Windows development host. These commands only inspect state:

```powershell
$PSVersionTable.PSVersion
git --version
python --version
docker version
docker info
ollama --version
ollama list
```

If a command is missing, record that fact; do not silently install unrelated dependencies. Check available RAM, free disk space, CPU/GPU and VRAM before choosing a model. Select the model only after this evidence is available.

Verify the checkout explicitly:

```powershell
git -C <EVORYN_CHECKOUT_PATH> status --short --branch
git -C <EVORYN_CHECKOUT_PATH> remote -v
git -C <EVORYN_CHECKOUT_PATH> branch --show-current
```

The path must point to the EVORYN clone, not ALF or Novaryn. Keep the current draft PR intact.

## Phase B: local model gate

1. Confirm Ollama is already installed and running, or install it only after recording host capabilities.
2. Choose a code-capable model based on available RAM/VRAM, license, context length, and measured quality. Do not copy a model name blindly from an old example.
3. Pull the selected model and run a small local prompt that asks it to explain a harmless test file.
4. Confirm the model endpoint is reachable from the intended agent runtime without exposing the service outside the host.
5. Record model name, model license, quantization, context window, resource use, and a small task-quality result.

Local inference can avoid per-token API billing, but is not cost-free: hardware, electricity, storage, and performance remain constraints.

## Phase C: OpenHands gate

Use the current official OpenHands local setup and Docker sandbox instructions. Prefer a disposable VM or dedicated development host. Do not start the service until the following are reviewed:

- exact current container image and digest/version;
- sandbox runtime and mounted volumes;
- whether Docker socket access grants host-level control;
- port binding is loopback-only;
- LLM endpoint is the local Ollama endpoint reachable from the container;
- no exchange secrets, Git write token, SSH keys, or unrelated repositories are mounted;
- the agent is limited to the EVORYN working tree and a dedicated branch.

First task after startup must be read-only: inspect the repository and report architecture gaps. Do not permit it to create orders, access exchange APIs, merge, deploy, or modify repository settings.

## Phase D: GitHub access gate

Initially, keep authenticated GitHub write access out of the agent. Use the already-created draft PR and human-operated GitHub workflow for review.

If later automation is justified:
- prefer a dedicated GitHub App or fine-grained token scoped to this repository;
- grant read access to repository contents and pull requests only when needed;
- grant contents/pull-request write only if a later, separately reviewed requirement demands it;
- do not grant Administration, Actions write, Secrets write, or Workflows write;
- never place a token in a tracked file, command transcript, model prompt, or agent-visible environment unless it is strictly required for the narrow task;
- require a human to review and merge.

A read-only token or no token is the default. GitHub repository instructions do not themselves grant or limit API permissions; actual GitHub installation/token scopes and repository settings enforce those boundaries.

## Phase E: acceptance evidence

Do not call the agent active until all are true:

- [ ] Actual Windows host reachable and environment inspected.
- [ ] EVORYN checkout path and Git remote verified.
- [ ] Local model starts and responds; model/version/license/resources recorded.
- [ ] Agent runtime starts successfully with Docker sandbox or an explicitly reviewed alternative.
- [ ] Agent can read and modify only a disposable EVORYN worktree.
- [ ] No exchange credentials or live-trading routes are available to the agent.
- [ ] GitHub permissions are read-only or absent by default.
- [ ] A harmless documentation-only test change is produced on a non-main branch.
- [ ] Diff and logs are inspected for secrets and unintended files.
- [ ] Human review is required; no automatic merge or deployment is enabled.

Record the actual command output and exact versions in the PR. If any gate fails, stop and report the blocker instead of weakening isolation.
