from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "research-validate-pr.yml"
SOURCE_TEST_WORKFLOW = (
    ROOT / ".github" / "workflows" / "research-source-tests.yml"
)
IMPACT_WORKFLOW = ROOT / ".github" / "workflows" / "research-impact.yml"
CODEOWNERS_TEMPLATE = ROOT / ".github" / "CODEOWNERS.template"


def test_workflow_runs_protected_base_and_never_checks_out_pr_content() -> None:
    content = WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request_target:" in content
    assert "permissions:\n  contents: read\n  pull-requests: read" in content
    assert "ref: ${{ github.event.pull_request.base.sha }}" in content
    assert "refs/pull/${RCP_PR_NUMBER}/head:refs/researchctl/pr-head" in content
    assert 'fetched_head" != "$RCP_HEAD_SHA' in content
    assert "researchctl\" ci dispatch" in content
    assert "--subject-head \"$RCP_HEAD_SHA\"" in content
    assert "--base-commit \"$RCP_BASE_SHA\"" in content
    assert "--head-ref \"$RCP_HEAD_REF\"" in content
    assert "--base-ref \"$RCP_BASE_REF\"" in content
    assert "github.event.pull_request.head.repo" not in content
    assert "checkout.*RCP_HEAD" not in content
    assert "RCP_SUBMISSION_ID" not in content
    assert "Submission PR branch" not in content
    assert "LINEAR" not in content
    assert "SSH_" not in content


def test_workflow_actions_are_pinned_and_codeowners_fails_closed() -> None:
    content = WORKFLOW.read_text(encoding="utf-8")
    action_references = re.findall(r"uses: ([^\s]+)", content)

    assert action_references
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", item) for item in action_references)
    assert "if [[ ! -s .github/CODEOWNERS ]]" in content
    assert ".github/CODEOWNERS.template" not in content


def test_source_tests_run_exact_pr_head_without_secrets_or_write_permission() -> None:
    content = SOURCE_TEST_WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request:" in content
    assert "pull_request_target:" not in content
    assert "permissions:\n  contents: read" in content
    assert "contents: write" not in content
    assert "pull-requests: write" not in content
    assert "secrets." not in content
    assert "name: researchctl/source-tests" in content
    assert "ref: ${{ github.event.pull_request.head.sha }}" in content
    assert "persist-credentials: false" in content
    assert 'observed_head" != "$RCP_HEAD_SHA' in content
    assert "RCP_BASE_SHA: ${{ github.event.pull_request.base.sha }}" in content
    assert 'git worktree add --detach "$RUNNER_TEMP/researchctl-base" "$RCP_BASE_SHA"' in content
    assert "pip install '.[dev]'" in content
    assert 'bin/python" -m pytest' in content
    assert 'doc tree \\' in content
    assert '--baseline-project "$RUNNER_TEMP/researchctl-base"' in content
    assert "ci dispatch" not in content
    assert "LINEAR" not in content
    assert "SSH_" not in content


def test_source_test_actions_are_pinned_and_check_identity_is_distinct() -> None:
    source_content = SOURCE_TEST_WORKFLOW.read_text(encoding="utf-8")
    exact_head_content = WORKFLOW.read_text(encoding="utf-8")
    action_references = re.findall(r"uses: ([^\s]+)", source_content)

    assert action_references
    assert all(
        re.fullmatch(r"[^@]+@[0-9a-f]{40}", item)
        for item in action_references
    )
    assert "name: researchctl/source-tests" in source_content
    assert "name: researchctl/exact-head" in exact_head_content
    assert "researchctl/source-tests" not in exact_head_content
    assert "researchctl/exact-head" not in source_content


def test_codeowners_template_has_no_claimed_real_owner() -> None:
    content = CODEOWNERS_TEMPLATE.read_text(encoding="utf-8")

    assert "/.research/decisions/" in content
    assert "/.research/reports/" in content
    assert "/.research/tasks/" in content
    assert "/.github/workflows/" in content
    assert "/.researchctl-docs.yaml" in content
    assert "/src/researchctl/" in content
    assert "/src/researchctl/services/ci_validation.py" in content
    owners = re.findall(r"(?m)^/\S+\s+(@\S+)$", content)
    assert owners
    assert all(owner.startswith("@REPLACE_WITH_") for owner in owners)


def test_main_push_impact_workflow_is_exact_and_uses_trusted_cli_entrypoint() -> None:
    content = IMPACT_WORKFLOW.read_text(encoding="utf-8")

    assert "push:\n    branches: [main]" in content
    assert "contents: write\n  pull-requests: write" in content
    assert "pull_request_target:" not in content
    assert "ref: ${{ github.event.after }}" in content
    assert 'observed_head" != "$RCP_AFTER' in content
    assert 'git update-ref refs/heads/main "$RCP_AFTER"' in content
    assert 'researchctl" ci impact' in content
    assert '--before "$RCP_BEFORE"' in content
    assert '--after "$RCP_AFTER"' in content
    assert '--generated-at "$RCP_GENERATED_AT"' in content
    assert "run.start" not in content
    assert "run retry" not in content
    assert "SSH_" not in content
    assert "LINEAR" not in content


def test_main_push_impact_workflow_pins_actions_and_serializes_main_scans() -> None:
    content = IMPACT_WORKFLOW.read_text(encoding="utf-8")
    action_references = re.findall(r"uses: ([^\s]+)", content)

    assert action_references
    assert all(
        re.fullmatch(r"[^@]+@[0-9a-f]{40}", item)
        for item in action_references
    )
    assert "group: research-impact-${{ github.ref }}" in content
    assert "cancel-in-progress: true" in content
