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
> notifications, credential-free post-merge shadow, and authenticated GitHub
> post-merge observation/enqueue are implemented and tested. Real repository
> protection, trusted scheduling and credentials, production Linear transport,
> and the measured shadow/canary pilot remain deployment work. No production
> latency or production-readiness claim is made.

## Workflow

```text
Agent work in a Session branch/worktree
  -> strict ExperimentPlan with accepted Task/policy value provenance
  -> deterministic no-fallback lint
  -> independent ephemeral read-only PlanReview
  -> deterministic reviewed RunSpec
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
   It sends the strict `SubmissionCreateRequest` and its Session capability to
   the trusted proposal host. That host runs the same Submission workflow,
   pushes the one derived branch, and creates or observes the deterministic PR
   as the accepted Agent App; it does not ask the human to open the PR.
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

Repository administrators can inspect whether those checks are actually merge
gates with a bounded read-only audit:

```bash
researchctl github doctor \
  --repository OWNER/REPOSITORY \
  --json
```

For a managed repository with accepted `ProjectPolicy.github`, use
`researchctl github doctor --project . --json`; the command derives the bound
repository/default branch and compares the observed gates with that policy.

Diagnose one blocked PR without changing GitHub state:

```bash
researchctl github pr-status \
  --repository OWNER/REPOSITORY \
  --pull-request NUMBER \
  --json
```

The typed result distinguishes a validator rejection from a missing/pending
check, review wait, governance gap, and a job that never acquired a runner. A
`ci_capacity_pending` result calls for billing or runner recovery, not disabling
the ruleset. GitHub Actions is the default event/check controller, but execution
may move to a dedicated self-hosted runner or external CI as long as it publishes
the authenticated required check for the exact head. See
`docs/runbooks/github-ci-execution.md` for the Manager procedure.

The command reads classic branch protection and active applicable branch
rulesets, checks the review, latest-head, status-check, force-push, and deletion
requirements, and exits `2` when a required gate is missing. It does not modify
GitHub or install an App. Project mode binds the audit to the accepted Agent App
and Manager policy, but proposal delivery still proves authorship only from the
created PR receipt; pre-create credential proof and accepting-reviewer proof
remain deployment work.

The rule installer is intentionally a different command. Its default invocation
is a read-only preview that emits the accepted-policy and current-observation
digests:

```bash
researchctl github apply-governance --project . --json
```

Only a human Manager may rerun it with `--apply` and both reviewed digests. The
command verifies the live `gh` user, rejects Session/App authority and ambiguous
ruleset/bypass configurations, applies the bounded classic protection payload,
and audits by read-back. No real GitHub rule was applied during repository
implementation.

The protected-base dispatcher currently recognizes Submission, generated Task
control, manager-owned Plan reviewer policy control, Report Impact, explicit
ImpactDecision, bootstrap proposal/acceptance, and manager-owned Linear policy
control changes. Unknown or mixed protocol mutations fail closed. Review the installed
single-maintainer CODEOWNERS baseline, require both checks, dismiss stale
approvals, and restrict protected-branch updates before treating merge as an
acceptance boundary. Required checks gate merge but do not trigger Actions. For
required review, use a distinct Agent GitHub App/bot as PR author and the human
manager as CODEOWNER; GitHub will not count the author's own approval.

## Plans Before Runs

`PLAN.yaml` is a strict generated-schema protocol record. Every field that can
change experiment semantics must cite the digest of an accepted Task or Project
policy containing that exact value. A drafting Agent cannot make a choice
authoritative by labeling its own YAML `user`; missing accepted intent becomes
`needs_input` rather than a provider, CLI, library, or environment fallback.

The manager first proposes an explicit reviewer provider/model policy. The
command changes only `plan_review` in the protected Project policy on a fixed
control branch; exact-head CI replays that field-level scope before merge:

```bash
researchctl plan configure-review \
  --provider codex \
  --model EXPLICIT_MODEL_ID \
  --policy-version plan-review-v1 \
  --timeout-seconds 60 \
  --expected-default-head FULL_DEFAULT_BRANCH_SHA
```

After the relevant Task choices and reviewer policy are accepted, the Agent
runs the deterministic and semantic gates before any Run side effect:

```bash
researchctl plan lint PLAN.yaml
researchctl plan review PLAN.yaml --output-file plan-review.yaml
researchctl plan compile PLAN.yaml \
  --review-file plan-review.yaml --output-file run-spec.yaml
researchctl run start --spec-file run-spec.yaml
```

The reviewer is a fresh attributed invocation with read-only access and no
second persistent RCP Session. Codex runs ephemerally; Claude runs in
bare non-persistent print/plan mode with tools, slash commands, and ambient MCP
configuration disabled. Reviewer credentials are
deployment inputs, not Plan fields. The environment strips GitHub, Linear,
Session-capability, and SSH-agent credentials. Self-review, reviewer-policy
drift, missing local review receipt, Plan/Task/policy digest drift, or a
non-passing opinion blocks compilation or execution. Plan-backed Submissions
carry separate `plan.yaml` and `plan-review.yaml` evidence, and protected CI
reruns deterministic validation from the protected Task and Project policy.

## Concise Research Writing

`AnalysisBrief` and `ResearchUpdate` are presentation contracts, not accepted
research authority. They keep Agent-authored analysis and operational updates
short before those texts enter a review document or Linear comment.

An `AnalysisBrief` contains one question, one `answer`, one protocol, up to
five metrics, up to eight `setting` rows, at most three interpretation points,
at most three material limitations, and explicit source references. Every
setting supplies the same metrics and references declared sources. Legacy YAML
may still use `conclusion`, but canonical schema and serialization use `answer`;
defining both is invalid. The generated JSON Schema exposes the answer's
2-sentence, 60-English-word, and 140-CJK-character lint limits. Display-sensitive
decimals such as `0.20` must be quoted because YAML numeric parsing cannot retain
trailing-zero precision.

```bash
researchctl brief lint analysis-brief.yaml
researchctl brief render analysis-brief.yaml --output-file analysis-brief.md
```

The Markdown renderer fixes the order to Answer, Evidence, Interpretation,
Limits, and Sources. Lint output reports both observed and maximum prose budgets,
for example `230/350 English words, 0/700 CJK characters`. The two language
measures are independent limits, not a shared converted budget. Generated output
carries renderer, source, and body digests. The source marker explicitly hashes
canonical validated model JSON, not the raw YAML file bytes. Re-rendering may
refresh an unedited renderer-owned file, while a body-digest mismatch still
rejects a manual edit.

`ResearchUpdate` represents exactly one operational delta: started, completed,
failed, or conclusion changed. It allows no more than two evidence values and
renders a compact heading/bullet update with a visible renderer marker:

```bash
researchctl update lint research-update.yaml
researchctl update render research-update.yaml
```

The update renderer produces a deterministic Linear-sized preview. Delivery
policy, issue binding, credentials, and accepted Report projection remain
separate concerns; passing writing lint does not authorize publication or make
the update canonical research truth.

## Portable Project Documents

Document contracts can be adopted without `researchctl init`. Put a standalone
policy in `.researchctl-docs.yaml`, protect that file with CODEOWNERS, and run
the same static checks locally or in any CI.

There are two policy versions. Version 2 is directory-first and is the
recommended model for a repository adopting document governance now; it is what
the commands below use. Version 1 is the classification-route model of ADR 0014,
is still fully supported, and is described under **Version 1 Route Policies** at
the end of this section. ADR 0017 records why the default moved.

```bash
researchctl doc policy-template --agent-format claude \
  --output-file .researchctl-docs.yaml
# Inventory the repository, then list the section directories it actually has.
researchctl doc policy-lint .researchctl-docs.yaml
```

`policy-template` defaults to `--policy-version 2`. What it writes is valid YAML
and every field except one is ready to adopt as rendered, but `sections` ships
empty and `policy-lint` deliberately refuses the candidate until you fill it in.
Directory names are facts about one repository, and a template that linted while
empty would invite copying another project's layout. The template is a starting
proposal, never an accepted taxonomy: the completed policy still goes through
manager/CODEOWNER review. `policy-lint` needs neither a Git repository nor
`researchctl init`.

A finished version 2 policy is short, and the directory names in it are the
whole taxonomy:

```yaml
version: 2
root: docs
sections:
  - path: runbooks
  - path: reference
  - path: experiments
    structured:
      contract: analysis-brief
root_pages: [README.md]
max_depth: 3
ownership:
  source: codeowners
  required: true
agent_guides:
  - path: CLAUDE.md
    format: claude
```

```bash
mkdir -p docs/runbooks docs/reference docs/experiments
researchctl doc agent-guide --project . --output-file CLAUDE.md
researchctl doc tree --project .
researchctl doc tree --project . --json
```

A section directory is the document type. Folders below a section organize or
version documents within that type and never create a new one; `max_depth`
bounds how deep they go. A document's title is its first level-one heading, its
owners come from CODEOWNERS, and the date it was last edited comes from Git. Tags
group documents for readers and grant no routing or approval authority.

Agents can discover exactly what this policy accepts without reading the
installed Python package:

```bash
researchctl doc contracts --project .
researchctl doc schema --contract simple-markdown-frontmatter
```

`doc contracts` lists the built-in authoring contracts; adding `--project .` also
reports, section by section, which of them your policy accepts. It fails with
`document_policy_missing` rather than describing a default layout when the
project has no policy.

An ordinary document is Markdown under the `simple-markdown-frontmatter`
contract. Frontmatter is optional and a document with none is valid; when
present it accepts only `status`, `tags`, `reviewed_on`, `locked`, `depends_on`,
and `superseded_by`. The version 1 keys it no longer accepts — `type`, `title`,
`owner`, `last_updated`, `validity`, and the `references`, `sources`,
`provenance`, and `relations` blocks — are each diagnosed with the replacement
that supersedes them, so an author migrating a file is told to delete `owner`
rather than only that it is forbidden.

Classification is a separate matter: it was never a frontmatter key. Under a
version 1 policy it is route metadata that lives in the policy file, and a
version 2 tree has no equivalent anywhere, because the section directory is the
taxonomy. A `classification` key written into frontmatter is diagnosed on that
basis.

```bash
researchctl doc scaffold --project . --type runbooks \
  --title "Operate the evaluation worker" \
  --output-file docs/runbooks/evaluation-worker.md
researchctl doc check docs/runbooks/evaluation-worker.md --project . --json

# A section that declared a structured contract keeps canonical YAML.
researchctl doc scaffold --project . --type experiments --contract analysis-brief \
  --title "Memory ceiling" --output-file docs/experiments/memory.yaml
researchctl doc check docs/experiments/memory.yaml --project . --json
researchctl doc render docs/experiments/memory.yaml --project . \
  --output-file docs/experiments/memory.md
researchctl doc tree --project . --json
```

Structured YAML is opt-in per section. Inside a section that declared one, the
canonical `.yaml` source is a direct child, its same-stem Markdown is renderer
output that is never hand-edited, and ordinary Markdown stays legal beside them.
A section that declares nothing holds only ordinary Markdown, which still
satisfies `simple-markdown-frontmatter`; what it needs is no structured YAML
contract, no canonical source, and no renderer. For a standalone AnalysisBrief
with no document policy, `brief lint` and `brief render` validate the model but
not repository placement.

RCP validates ordinary Markdown and never rewrites it. There is no formatter for
prose: the only Markdown bytes the tool owns are renderer output beside a
canonical YAML source and the managed block inside a configured Agent guide. The
renderer itself is a thin optional projection from a validated typed model to
stable Markdown source, owning deterministic section order, tables, provenance
markers, and source/body consistency. It is not a general Markdown parser, HTML
renderer, theme system, or documentation-site framework; projects may use mature
Markdown tooling for those downstream jobs while RCP remains the schema/lint
authority.

For a browsable document library, install the optional MkDocs adapter and build
from an ephemeral validated manifest:

```bash
pip install 'research-control-plane[docs-site]'
researchctl doc site-manifest --project . --require-clean \
  --output-file /tmp/researchctl-site-manifest.json
researchctl doc schema --contract simple-document-site-manifest
RESEARCHCTL_SITE_MANIFEST=/tmp/researchctl-site-manifest.json \
  mkdocs build --strict --site-dir build/site
```

```yaml
# mkdocs.yml: theme and presentation settings live here; do not hand-write nav.
site_name: Project documents
docs_dir: docs
plugins:
  - search
  - researchctl:
      manifest: !ENV RESEARCHCTL_SITE_MANIFEST
      require_clean: true
```

`doc site-manifest` first requires the complete governed tree to pass, then emits
the manifest kind its policy version produces: `simple-document-site-manifest`
for version 2 and `document-site-manifest` for version 1. Both are strict,
deterministic, engine-neutral JSON. What they genuinely share is the document
root, repository identity and clean/dirty state, a policy digest, an ordered page
list with each page's kind, title, canonical-source path where one exists, and
exact content and source digests, plus deliberate exclusions and a
self-authenticating manifest digest.

Beyond that the two differ. A version 2 page carries its section, owners, tags,
review date, Git edit time, and either an ordinary `status` or a structured
`lifecycle`; a version 1 page carries route order, document type,
classification, relations, and either a `validity` or a structured `lifecycle`,
and has no owners field at all, because ownership under that model is a
frontmatter string rather than a resolved fact. Only the version 2 manifest
enumerates static assets, and that enumeration is what makes its published set a
closed world: with every page and asset listed, the adapter can reject anything
else in the document root, while a version 1 build retains unlisted static files
it cannot enumerate.

The manifest is a replaceable build artifact, not tracked authority; write it
outside the repository or to an ignored build directory.

The plugin reads only the manifest and verifies every byte it names, refusing
dirty publication when configured. Under a version 2 manifest it verifies pages,
canonical sources, and assets, drops the exclusions — canonical YAML and any
CODEOWNERS file inside the document root — rejects anything else there that the
manifest did not list, builds nested navigation from the section order, strips
optional frontmatter before rendering, and injects display-only owner, review,
edit, tag, status, and lock metadata. Under a version 1 manifest it keeps its
original behaviour: route-order navigation, route metadata, exclusion of
declared paths, rejection of unlisted Markdown, and retention of unlisted static
files, which that manifest cannot enumerate. Both add an immutable source link
when the remote is recognized. MkDocs owns Markdown-to-HTML, search, live reload,
and themes. GitHub Pages or Read the Docs may host the strict build from
protected `main`, but neither owns taxonomy or acceptance. No `mkdocs.yml nav`
becomes a second directory truth, and MkDocs remains absent from the core
install.

This repository keeps that presentation-only configuration in `mkdocs.yml` and
reuses the existing `researchctl/source-tests` runner for the strict build. The
canary does not create another Actions job and does not deploy Pages. The
CODEOWNERS rule covers `mkdocs.yml` because a future accepted publication must
not let an unreviewed presentation change hide validated pages.

Standalone Agents need a repository-local discovery surface, so the policy
declares one or more managed guide targets:

```yaml
agent_guides:
  - path: CLAUDE.md
    format: claude
  # - path: AGENTS.md
  #   format: agents
```

`doc agent-guide` inserts or refreshes one deterministic managed block and leaves
every other instruction in the file untouched. The block states that the section
directory is the type, that CODEOWNERS and Git own ownership and edit time, which
optional frontmatter fields exist, which sections accept a structured contract,
and which commands to run; it carries a visible renderer marker. `doc tree`
fails when a configured guide is missing, unreadable, or stale, so CI checks the
instructions and the documents together. Writes are limited to targets the
protected policy declares, and both policy versions share one marker identity, so
upgrading a policy replaces the same block instead of leaving two behind.

`researchctl doctor` recognizes a standalone policy when neither `.research` nor
`.researchctl.toml` exists. In that valid mode it runs policy/tree checks and
marks managed Project, Session, record, and generated-schema checks as not
applicable instead of emitting missing-schema errors.

A policy is required. If neither a standalone nor a managed Project policy
exists, the document commands fail with `document_policy_missing`; they never
silently choose a hierarchy. A one-off caller may pass `--policy-file` instead.

These standalone document commands open no `.research`, SQLite, Session, or
manager state. Version 2 tree lint checks sections and depth, CODEOWNERS
resolution and required ownership, first-heading titles, optional frontmatter
and its superseded keys, `depends_on` and `superseded_by` targets, links,
structured YAML/Markdown pairs and renderer bytes, static assets, and managed
guide freshness. An optional baseline checkout additionally enforces byte
immutability:

```bash
researchctl doc tree --project . --baseline-project /path/to/base-checkout
```

The baseline reader is deliberately version-blind. It validates only the
baseline's document-root path and then scans raw Markdown frontmatter below it
for either immutability marker, `locked: true` or the version 1
`validity: frozen`, so a policy upgrade in the same change set cannot release a
document the protected base had immobilized. Unsafe policy paths, malformed
baseline YAML or frontmatter, and changed or deleted immobilized files fail
closed.

Paths written inside a document, including every `depends_on` and
`superseded_by` target, are repository-root relative. Human CLI errors include
invalid field paths and available YAML line/column locations by default; `--json`
exposes the same details for automation.

The policy-adoption PR should attach the exact `doc policy-lint` result and the
`doc tree --json` envelope. Review automation should consume that JSON rather
than a manually paraphrased pass/fail statement. An Agent may author documents
and push them to a proposal branch, but repository CI, CODEOWNER review, and a
protected merge decide acceptance; changing the policy, the sections, a
structured contract, CODEOWNERS, or a managed guide is a governance change that
cannot ride inside a content proposal.

### Version 1 Route Policies

A repository already on the classification-route model keeps working unchanged,
and a new one may still choose it:

```bash
researchctl doc policy-template --policy-version 1 --agent-format claude \
  --output-file .researchctl-docs.yaml
```

That renders the original candidate byte for byte. Every route in it carries a
structured `rationale` placeholder, and `policy-lint` rejects those template
values until the author cites the existing project artifacts that justify each
retained or replacement route.

Under a version 1 policy a route is the exact five-part mapping of
classification, document type, contract, directory, and rationale. Ordinary
Markdown uses strict frontmatter with `type`, `title`, `owner`, `last_updated`,
and `validity`; classifications use canonical `a/b:c` labels; `classification_depth`
bounds the namespace segments before `:` (`minimum: 2`, `maximum: 4` by default)
while the independent `max_depth` bounds filesystem nesting below a route
(`1..8`, default `4`). None of that applies to a version 2 tree.

Version 1 also keeps the features built around routes: `doc index` renders the
configured type/classification/contract/directory table, `machine_artifact_roots`
hold stable machine inputs such as `data/*.json` under explicit extension
allowlists that can never permit Markdown, `validity: frozen` marks immutability,
and a structured route may declare `generated_markdown_frontmatter.required_fields`
so RCP preserves a project-owned envelope byte for byte around a generated body.
Manual provenance `value` fields there are exact display strings, must be quoted
when they look numeric (`value: "91.20"`), and must appear verbatim in the body.

After `researchctl init`, the policy lives under
`.research/policies/default.yaml.document_layout`. Its unversioned default is
version 1, while `version: 2` selects the directory-first model. Changing either
layout uses manager-only `researchctl doc configure-layout`, and protected-base
CI verifies that no other Project policy field changed. An existing managed
version 1 repository can therefore migrate through the same reviewed control
proposal rather than through a second mutation path.

The two policy sources cannot coexist: a managed repository that also defines
`.researchctl-docs.yaml` fails with `document_policy_shadowed`. Version 2 is
available from either source, but only the managed source carries accepted
Project policy authority. `doc index` remains version 1-only; version 2 uses the
site manifest instead of projecting a route table it does not have.

Editor and Agent integrations should consume `doc schema`, `doc check --json`, or
`doc tree --json` rather than implementing a second validator. `doc contracts`
lists the five authoring contracts: `markdown-frontmatter`,
`simple-markdown-frontmatter`, `analysis-brief`, `design-document`, and
`project-status-summary`. `doc schema --contract` prints those five and, in
addition, `simple-document-layout-policy`, `document-site-manifest`, and
`simple-document-site-manifest`; the legacy `document-layout-policy` schema is
generated but not offered through CLI discovery. A reusable Skill may teach the
generic command workflow and an MCP adapter may expose it remotely, but taxonomy
remains in the repository policy and enforcement remains in the CLI/CI core. The
portable contract is recorded in ADR 0014 and the directory-first model in
ADR 0017.

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

`researchctl-github-post-merge` is the one-shot authenticated enqueue adapter.
It uses an authenticated `gh api` environment to bind one merged PR to the
fixed workflow, exact check run, and exact attestation artifact before it may
write the durable outbox event. Artifact-derived local requests remain
shadow-only and cannot self-assert authenticated provenance.

The repository does not yet ship a production Linear API/MCP adapter or a
deployed webhook/poller installation. The GitHub adapter is also not scheduled
or credentialed merely because its code is installed. Until those deployment
adapters can verify stable internal `author_app_id` values, fake-port tests and
local shadow runs are not a claim of live Linear integration.

## Implemented Boundary

Implemented and tested locally:

- safe existing-repository initialization, doctor, protocol compatibility, and
  isolated bootstrap acceptance;
- manager-owned Task proposals, guided Task creation, and Agent capability
  denials;
- Codex/Claude Session worktrees, tmux identity, attach/pause/continue, status,
  grouped exception inbox, notification inbox, and terminal fallback;
- frozen local Runs, preflight, attempts/retries, immutable provenance,
  collection, and read-only reconciliation;
- strict ExperimentPlan and PlanReview schemas, accepted-value provenance,
  no-fallback lint, explicit manager-owned reviewer policy control, independent
  ephemeral review, deterministic RunSpec compilation, Run gating, separate
  Submission evidence, and protected-CI replay;
- portable standalone/managed project-document schemas, classification routes,
  frontmatter/tree lint, deterministic render/index checks, frozen-baseline
  enforcement, machine-artifact boundaries, and manager-owned layout control;
- Submission, Decision, deterministic Report rendering, manager acceptance,
  constrained Agent-authored GitHub PR delivery, protected-base dispatch,
  exact-head validation, and source tests;
- manager/trusted-automation Report Impact proposals with strict code-path
  dependencies, optimistic concurrency, merge-triggered all-Report batching,
  fixed GitHub delivery, clean-runner replay, exact-head regeneration,
  effective `report status` reads, and manager-only rerun/waive/keep-stale/
  invalidate/dependency-fix Decision PRs; no Impact or decision path launches
  an experiment;
- accepted-merge Git-object validation, credential-free post-merge shadow,
  authenticated GitHub observation/enqueue, Linear outbox/worker state
  machines, ingress grammar, and receipt lineage.

Still gated deployment or later-phase work:

- executing and canarying the reviewed branch-rule preview for the installed
  CODEOWNERS baseline, GitHub App proposal/post-merge credential installation,
  a live App-authored PR pilot, and trusted scheduling;
- live Codex/Claude Plan-review invocation and credential/configuration canary
  against the explicitly selected deployment models;
- real Linear transport under the deployment credential and a measured shadow
  then allowlisted canary pilot;
- SSH fleet execution and cross-host artifact staging;
- trusted live resource/environment Impact providers, protected provider replay,
  and safe batch worktree synchronization;
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
researchctl-github-post-merge --help
```

The authoritative contracts are
[RESEARCH_CONTROL_PLANE_SPEC.md](docs/RESEARCH_CONTROL_PLANE_SPEC.md), the
accepted [ADRs](docs/adr), [MUTATION_CONTRACTS.md](docs/MUTATION_CONTRACTS.md),
the audited [WORKFLOW_COVERAGE.md](docs/WORKFLOW_COVERAGE.md), and
[TRACEABILITY_MATRIX.md](docs/TRACEABILITY_MATRIX.md). Development and tests
must write only inside the checked-out workspace or explicit temporary
directories; external reference repositories are read-only inputs.

Contributions follow [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities
through [SECURITY.md](SECURITY.md); participation is governed by
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). The project is licensed under the
[MIT License](LICENSE).
