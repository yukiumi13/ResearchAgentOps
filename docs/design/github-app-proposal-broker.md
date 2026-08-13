# GitHub App proposal broker

> Renderer: `researchctl-renderer:research-design-document.v2`
<!-- researchctl-generated:research-design-document.v2;source=sha256:c51232f32b0ba7f591337e1eb26e3ee7937ee6b6807c8a75b7027e471dbbd7a7;body=sha256:69f20ffd044c281e76895479942aac620324372bfa4081d48d691ac78cee48a7 -->

- Document: `document_20260813T032815Z_f40b3286bd837194b1c089c8`
- Classification: `design/architecture:proposal`
- Status: `draft`
- Revision: `1`
- Basis commit: `7bd5309acfc136176c8cab7e57c83eacfa652535`
- Author: `codex-document-dogfood` (`external_agent`)
- Updated: `2026-08-13T03:28:15+00:00`

## Problem

Proposal delivery verifies the observed pull-request bot author, but the current Session path can still use the human user's Git credential for the branch push and can receive a reusable GH\_TOKEN. That does not prove App identity before mutation and exposes more credential authority than a Session needs.

## Context

GitHubGovernancePolicy already binds the repository, default branch, App and installation IDs, canonical bot login, human Managers, and required gates. SubmissionWorkflowService already owns proposal validation, canonical branch derivation, rendering, event receipts, and uncertain-outcome replay. The new boundary should supply credentials to that workflow without creating another Submission state machine.

## Goals

- Keep the App private key and installation token outside every Agent process and receipt.
- Use one verified installation token for both the exact Git push and pull-request API call.
- Reuse existing proposal validation, canonical rendering, idempotency, and observe-before-retry behavior.
- Bind every delivery receipt to the accepted policy, repository, App installation, base, head, branch, and observed bot author.

## Non-Goals

- Create or install the GitHub App without repository-owner consent.
- Grant the App review, merge, branch-protection, Actions-write, Checks-write, or bypass authority.
- Add a general remote-execution API, message broker, database, or second proposal state machine.

## Constraints

- The trusted host must re-read accepted ProjectPolicy and repository state rather than trust caller-supplied repository or ref values.
- Token lifetime and requested repository permissions must be bounded and verified from GitHub responses.
- A timeout after push or pull-request creation must trigger observation before retry.
- Git authentication must not fall back to the Manager's SSH agent, credential helper, or HOME configuration.

## Options

### One-shot trusted host around the existing Submission workflow

Disposition: `selected`

Rationale: This is the smallest boundary that controls both credential issuance and mutation while retaining one proposal implementation.

Benefits:
- Reuses the existing delivery port and operation journal.
- Keeps secret lifetime bounded to one process and one request.
- Can later sit behind a Unix socket without changing domain behavior.

Drawbacks:
- Requires a separately protected runtime identity and repo-external private-key path.

### Mint a token and export it to the Agent Session

Disposition: `rejected`

Rationale: It does not establish the required credential boundary.

Benefits:
- Requires little new host code.

Drawbacks:
- The Agent can reuse the token for unreviewed GitHub operations during its lifetime.
- Git push may still fall back to a human SSH credential.

### Implement proposal preparation and delivery again in a broker service

Disposition: `rejected`

Rationale: Deployment isolation does not justify duplicating domain authority.

Benefits:
- Allows a standalone deployment surface.

Drawbacks:
- Duplicates scope validation, rendering, idempotency, and recovery logic.
- Creates a second proposal state machine that can drift from researchctl submit.

## Components

### `app-token-issuer`

Read the protected private key, sign a short-lived App JWT, request one repository-scoped installation token, and verify the response.

Interfaces:
- GitHub App installation-token API

### `trusted-proposal-host`

Authenticate the Session capability, load accepted policy, invoke the existing Submission workflow, and return a secret-free receipt.

Interfaces:
- SubmissionCreateRequest on standard input
- ApplicationService submission operation journal

### `isolated-git-transport`

Push only the canonical exact proposal ref over HTTPS without HOME, SSH\_AUTH\_SOCK, or persistent credential storage.

Interfaces:
- SubmissionDeliveryPort

### `github-observer`

Verify installation identity, repository selection, permissions, remote head, PR metadata, and canonical bot author.

Interfaces:
- GitHub REST API

## Workflows

### App-authored proposal

1. Agent submits the existing strict SubmissionCreateRequest with its Session capability.
2. Trusted host authenticates the Session and re-runs proposal preparation against accepted state.
3. Token issuer obtains and verifies one short-lived installation token for the policy-bound repository.
4. Isolated transport pushes the exact derived commit to its canonical proposal ref and observes the remote head.
5. Existing delivery logic creates or observes the deterministic pull request and verifies the configured bot author.
6. Host discards credentials and returns a receipt containing only identities, digests, refs, commit, PR number, and observation time.

### Ambiguous remote result

1. Record the stage whose mutation result is uncertain without marking the operation terminal.
2. Re-run the same request and observe the exact remote branch or pull request before any retry.
3. Accept only the exact policy-bound identity or fail closed on conflict.


## Security

- Run the trusted host under a principal that the Agent cannot inspect or signal; same-UID environment filtering is mistake containment only.
- Require a regular non-symlink private-key file outside the repository with owner-only permissions and never serialize its bytes.
- Sanitize subprocess environments and disable persistent Git credential helpers, terminal prompts, SSH fallback, and shell execution.
- Verify the App and installation identities plus Contents and Pull requests permissions before performing remote mutation.
- Keep the App outside CODEOWNERS, Managers, branch-rule bypass actors, and protected-main write access.

## Failure Modes

- **Condition:** Private key, App identity, installation identity, selected repository, or permission response differs from accepted policy.
  **Behavior:** Fail before Git or pull-request mutation and expose no remote secret response body.
  **Recovery:** Correct the owner-reviewed App installation or governance policy and submit a fresh request.
- **Condition:** Installation-token acquisition times out or returns malformed, oversized, expired, or over-scoped data.
  **Behavior:** Fail without returning or persisting a token.
  **Recovery:** Observe App installation state and retry token acquisition only after the cause is understood.
- **Condition:** Push or pull-request creation has an ambiguous result.
  **Behavior:** Leave the journaled operation running and never assume success or repeat blindly.
  **Recovery:** Replay the identical operation so existing delivery logic observes the exact effect first.
- **Condition:** The remote branch or PR exists with a different head, metadata, repository, base, or author.
  **Behavior:** Fail closed as an identity conflict.
  **Recovery:** Manager investigates and closes or removes the conflicting unauthorized proposal explicitly.

## Migration

- Create and install the least-privilege GitHub App on ResearchAgentOps under repository-owner consent.
- Accept a ProjectPolicy.github value containing the observed App, installation, bot, Manager, branch, and check identities.
- Implement and adversarially test the one-shot trusted host and App token issuer using fake GitHub and Git transports.
- Run one App-authored canary PR with branch protection still advisory and verify the full secret-free receipt.
- Apply and audit protected-main governance, then repeat stale-review, latest-head, and post-merge identity tests.

## Validation

- **Case:** Credential non-disclosure
  **Expected:** Agent environment, argv, stdout, stderr, journal, and receipt contain no private key, JWT, installation token, or human Git credential.
  **Evidence:** tests/unit/test\_github\_app\_broker.py
- **Case:** Exact App identity
  **Expected:** Token, selected repository, permissions, pushed ref, pull-request author, and policy all bind the same configured App installation and exact commit.
  **Evidence:** tests/unit/test\_github\_app\_broker.py
- **Case:** Idempotent uncertain recovery
  **Expected:** Timeout-after-effect is observed and reused; timeout-without-observable-effect remains uncertain.
  **Evidence:** tests/unit/test\_github\_submission.py
- **Case:** Live ResearchAgentOps canary
  **Expected:** App authors a proposal that only current checks plus a latest-head yukiumi13 CODEOWNER review can accept.
  **Evidence:** Deployment receipt to be recorded after owner installation and canary execution.

## Open Questions

- Whether the first deployment uses a separate Unix user directly or a container with a narrow Unix-socket frontend.

## Decisions Needed

- Should the first implementation use the selected one-shot trusted-host boundary before adding a persistent socket service?
  - Accept the one-shot host for the first live App canary.
  - Require a separately reviewed persistent broker design first.

## Sources

- `identity-adr`: `docs/adr/0015-github-proposal-identity-and-protected-acceptance.md`
- `submission-delivery`: `src/researchctl/adapters/github_submission.py`
- `session-host`: `src/researchctl/session_host.py`
