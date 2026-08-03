# Research Control Plane

Research Control Plane (RCP) is a local-first Agent harness and lightweight
research control plane for Codex and Claude. It coordinates isolated Agent
Sessions, immutable experiment evidence, human acceptance, exact-head CI, and
optional Linear projection around an existing Git repository.

RCP is not an Agent model, an experiment framework, a distributed scheduler, or
a replacement for Git, GitHub, SSH, tmux, and repository-native launchers. The
core deliberately uses one Python application service, one host-local SQLite
database, Git worktrees, and short-lived integration jobs. It has no required
HTTP service, broker, custom web UI, or always-on Agent daemon.

> **Status: pre-release local implementation.** The local harness, immutable
> Runs, deterministic Reports, two-check CI contract, durable Session
> notifications, and credential-free post-merge shadow are implemented and
> tested. Real repository protection, authenticated GitHub post-merge ingress,
> production Linear transport, and the measured shadow/canary pilot remain
> deployment work. No production latency or production-readiness claim is made.

## Workflow

```text
Agent work in a Session branch/worktree
  -> immutable research-run/<run_id> evidence
  -> ResearchSubmission
  -> manager review accept creates a candidate Decision + Report
  -> researchctl/source-tests executes the exact PR source
  -> researchctl/exact-head validates protocol and generated Report bytes
  -> current CODEOWNER approval + protected merge
  -> Decision + accepted Report become authoritative in Git
  -> trusted post-merge validation and optional outbox enqueue
  -> trusted Linear app projects the same Report and stores a receipt
```

Humans, Agents using strict JSON, and Python automation call the same
`ApplicationService`, request models, authorization checks, and state
transitions. JSON is a presentation contract, not a second business API.

## Authority

| Data | Authority | Purpose |
|---|---|---|
| Project, Task, policy, Decision, accepted Report | Protected default branch | Reviewable research truth |
| Source proposal | Session branch and isolated worktree | One Agent's unaccepted changes |
| Frozen RunSpec and collected RunResult | Immutable `research-run/<run_id>` ref | Reproducible provenance |
| Session lifecycle, operation journal, notifications, outbox, claims, receipts | Host-local SQLite | Durable live operational state |
| PR, review, and required checks | GitHub | Review and merge transport |
| Comments and addressed requests | Linear | Non-authoritative projection and transport |

SQLite does not own Reports or scientific dependency truth. It stores durable
runtime facts such as operation idempotency, Session observations, ingress
receipts, notification revisions, delivery attempts, leases, dead letters, and
projection receipts. Some undelivered inbox/outbox state cannot be reconstructed
from Git alone. Production use therefore requires persistent storage, one
writer, consistent backups, and a tested restore procedure.

## Reports And CI

A final Report is not authored by CI or invented after merge:

1. The Agent proposes a canonical Submission and evidence.
2. `researchctl review accept` is manager-only and materializes a candidate
   Decision, Report YAML, Report Markdown, and credential-free Linear preview
   on the exact proposal head.
3. `researchctl/exact-head` regenerates those outputs from canonical records and
   compares them byte for byte while binding the result to the exact head/tree.
4. The protected merge makes that already-validated Report accepted Git truth.
5. Post-merge automation revalidates the accepted merge and projects the same
   deterministic content to Linear. It does not rewrite or summarize it again.

Two independent required checks cover different risks:

| Check | Trigger and trust | What it proves |
|---|---|---|
| `researchctl/source-tests` | `pull_request`; exact PR head; read-only and credential-free | The source being proposed passes the repository test suite |
| `researchctl/exact-head` | `pull_request_target`; validator installed from protected base; PR head read only as Git objects | Supported RCP protocol changes and generated outputs match the exact proposed head |

For an ordinary source-only PR, exact-head returns `not_applicable`. That means
no governed RCP protocol path changed. It is not a source test, and cannot
replace `researchctl/source-tests`.

The protected-base dispatcher currently recognizes Submission, generated Task
control, bootstrap proposal/acceptance, and manager-owned Linear policy control
changes. Unknown or mixed protocol mutations fail closed. Review the installed
single-maintainer CODEOWNERS baseline, require both checks, dismiss stale
approvals, and restrict protected-branch updates before treating merge as an
acceptance boundary.

## Session Addressing

Sessions do not need email addresses, Linear accounts, or one external user per
Agent. One trusted app identity, configured per deployment and shown here as
`researchctl-app`, owns transport. The comment body names the exact local
Session and commit:

```text
@researchctl-app notify session:<full-session-id> commit:<full-commit-sha>
Please inspect this exact commit and reply with the evidence path.
```

In a thread already bound by a verified RCP delivery receipt, the manager can
ask the originating Session to follow up without repeating its ID:

```text
@researchctl-app reply commit:<full-commit-sha>
Please re-check this exact commit.
```

The trusted ingress verifies the workspace, issue, thread, stable app and author
IDs, Task binding, full Session ID, exact Session branch, and commit reachability.
It never trusts a display name or a copied hidden marker. The request becomes a
durable, non-authoritative `SessionNotification`. An active Session lists and
acknowledges the inbox at safe checkpoints; stopped or lost Sessions route to a
manager exception inbox rather than disappearing.

```bash
researchctl session list
researchctl session show SESSION_ID
researchctl session address SESSION_ID --commit FULL_COMMIT_SHA \
  --app researchctl-app
researchctl notification list
researchctl notification ack NOTIFICATION_ID --expected-revision REVISION
researchctl notification reply NOTIFICATION_ID \
  --expected-revision REVISION --body 'Reviewed; evidence is consistent.'
```

RCP does not inject keystrokes or arbitrary text into a model while it is
generating or running an experiment. Persistence proves delivery to the inbox;
only `ack` or `reply` proves the Session application consumed the revision.

## Linear Delivery

The repository includes deterministic accepted-result and Session-reply events,
stable markers, target preflight contracts, durable claims, retry/dead-letter
state, crash-after-create recovery, and lineage-rich receipts. Every projected
comment records Agent, Session, Task, optional Report, accepted merge, exact-head
attestation, renderer, and payload identities while the visible author remains
the trusted app.

A manager prepares the complete non-secret Linear target and notification
allowlist as a reviewed control proposal. The command does not contact Linear,
merge its own proposal, or accept credentials:

```bash
researchctl linear configure \
  --policy-file linear-policy.yaml \
  --expected-default-head FULL_DEFAULT_BRANCH_SHA
```

`researchctl-linear-host shadow` is credential-free. It validates a local
dispatch artifact against an accepted Git merge and emits a canonical
observation. It performs no network call and writes no live outbox row. Enqueue
requires authenticated GitHub provenance with workflow, check, and artifact
identities; only a later trusted delivery worker may use the Linear credential.

The repository does not yet ship a deployed GitHub artifact-authentication
adapter, a production Linear API/MCP adapter, or a webhook/poller installation.
Until those deployment adapters are configured and can verify stable internal
`author_app_id` values, tests with fake ports and local shadow runs are not a
claim of live Linear integration.

## Implemented Boundary

Implemented and tested locally:

- safe existing-repository initialization, doctor, protocol compatibility, and
  isolated bootstrap acceptance;
- manager-owned Task proposals and Agent capability denials;
- Codex/Claude Session worktrees, tmux identity, attach/pause/continue, status,
  notification inbox, and terminal fallback;
- frozen local Runs, preflight, attempts/retries, immutable provenance,
  collection, and read-only reconciliation;
- Submission, Decision, deterministic Report rendering, manager acceptance,
  protected-base dispatch, exact-head validation, and source tests;
- accepted-merge Git-object validation, credential-free post-merge shadow,
  Linear outbox/worker state machines, ingress grammar, and receipt lineage.

Still gated deployment or later-phase work:

- branch rules and reviewer-policy verification for the installed CODEOWNERS
  baseline, GitHub post-merge artifact authentication, and trusted scheduling;
- real Linear transport under the deployment credential and a measured shadow
  then allowlisted canary pilot;
- SSH fleet execution and cross-host artifact staging;
- dependency impact and safe batch worktree synchronization;
- mobile/chat views and a shared GPU controller, only if operational evidence
  justifies their additional complexity.

See [DESIGN_ASSESSMENT.md](docs/DESIGN_ASSESSMENT.md) for maintainability,
extensibility, response-time, usability, failure simulations, and rollout gates.
The [scenario catalog](docs/USER_SCENARIOS.md) maps all 77 private historical
requirements to 33 public acceptance scenarios without publishing the source
conversation. Static traceability is requirement coverage, not a claim that all
later deployment scenarios already pass.

## Install And Develop

Python 3.12 is required:

```bash
python -m pip install -e '.[dev]'
python -m pytest
researchctl --help
researchctl-linear-host --help
```

The authoritative contracts are
[RESEARCH_CONTROL_PLANE_SPEC.md](docs/RESEARCH_CONTROL_PLANE_SPEC.md), the
accepted [ADRs](docs/adr), [MUTATION_CONTRACTS.md](docs/MUTATION_CONTRACTS.md),
and [TRACEABILITY_MATRIX.md](docs/TRACEABILITY_MATRIX.md). Development and tests
must write only inside the checked-out workspace or explicit temporary
directories; external reference repositories are read-only inputs.

Contributions follow [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities
through [SECURITY.md](SECURITY.md); participation is governed by
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). The project is licensed under the
[MIT License](LICENSE).
