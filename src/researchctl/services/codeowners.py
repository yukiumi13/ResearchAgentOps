"""Read repository ownership from GitHub's CODEOWNERS file.

CODEOWNERS is the only ownership statement GitHub actually enforces at merge
time, so it is the single source of truth here. A second owner map in document
policy or frontmatter would inevitably drift from the enforced one, which is
exactly the duplicate truth this contract exists to prevent.

Researchctl only ever reads this file. It never writes, rewrites, or proposes a
change to CODEOWNERS, and resolving an owner grants no authority: it is display
metadata derived from the file that governs review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pathspec.patterns.gitignore import GitIgnorePatternError
from pathspec.patterns.gitignore.basic import GitIgnoreBasicPattern

from researchctl.errors import RCPError
from researchctl.repository import safe_repository_path

#: GitHub's discovery order. The first existing file wins; the rest are ignored.
CODEOWNERS_LOCATIONS: tuple[str, ...] = (
    ".github/CODEOWNERS",
    "CODEOWNERS",
    "docs/CODEOWNERS",
)

#: GitHub refuses to load a CODEOWNERS file of 3 MiB or more.
CODEOWNERS_MAX_BYTES = 3 * 1024 * 1024

_TEAM = re.compile(r"^@[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?$")
_EMAIL = re.compile(r"^[^@\s]+@[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}$")
#: A GitLab section header occupies the whole line, unlike a bracket range.
_SECTION_HEADER = re.compile(r"^\^?\[[^\[\]]+\](?:\[\d+\])?$")


@dataclass(frozen=True, slots=True)
class CodeownersProblem:
    """One actionable defect in the CODEOWNERS file."""

    line: int
    message: str


@dataclass(frozen=True, slots=True)
class CodeownersRule:
    line: int
    pattern: str
    owners: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CodeownersRuleset:
    """Parsed rules plus every defect found while parsing them."""

    path: str
    rules: tuple[CodeownersRule, ...]
    problems: tuple[CodeownersProblem, ...]

    def owners_for(self, repository_relative_path: str) -> tuple[str, ...]:
        """Resolve owners with GitHub's last-matching-rule precedence."""

        for rule in reversed(self.rules):
            if _matcher(rule.pattern).match_file(repository_relative_path):
                return rule.owners
        return ()


_MATCHER_CACHE: dict[str, GitIgnoreBasicPattern] = {}


def _matcher(pattern: str) -> GitIgnoreBasicPattern:
    cached = _MATCHER_CACHE.get(pattern)
    if cached is None:
        cached = GitIgnoreBasicPattern(pattern)
        _MATCHER_CACHE[pattern] = cached
    return cached


def _valid_owner(token: str) -> bool:
    return bool(_TEAM.match(token) or _EMAIL.match(token))


def parse_codeowners(text: str, *, path: str) -> CodeownersRuleset:
    """Parse CODEOWNERS into ordered rules, collecting every syntax defect."""

    rules: list[CodeownersRule] = []
    problems: list[CodeownersProblem] = []
    for index, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _SECTION_HEADER.match(line):
            problems.append(
                CodeownersProblem(
                    line=index,
                    message=(
                        "Section headers are a GitLab extension that GitHub ignores; "
                        "remove this line."
                    ),
                )
            )
            continue
        pattern, *owners = line.split()
        if pattern.startswith("!"):
            problems.append(
                CodeownersProblem(
                    line=index,
                    message="GitHub CODEOWNERS does not support negated patterns.",
                )
            )
            continue
        if "[" in pattern or "]" in pattern:
            # pathspec would happily compile a character range, so rejecting it
            # here is what keeps resolution equal to GitHub's.
            problems.append(
                CodeownersProblem(
                    line=index,
                    message=(
                        f"Pattern {pattern!r} uses bracket syntax; GitHub CODEOWNERS "
                        "does not support character ranges such as [a-z]."
                    ),
                )
            )
            continue
        if pattern.startswith("\\#"):
            problems.append(
                CodeownersProblem(
                    line=index,
                    message=(
                        f"Pattern {pattern!r} escapes a leading '#'; GitHub CODEOWNERS "
                        "reads such a line as a comment and cannot match it."
                    ),
                )
            )
            continue
        try:
            compiled = _matcher(pattern)
        except (GitIgnorePatternError, ValueError, TypeError) as error:
            problems.append(
                CodeownersProblem(
                    line=index,
                    message=f"Pattern {pattern!r} is not a usable path pattern: {error}",
                )
            )
            continue
        if compiled.include is not True:
            problems.append(
                CodeownersProblem(
                    line=index,
                    message=f"Pattern {pattern!r} does not select any path.",
                )
            )
            continue
        invalid = [token for token in owners if not _valid_owner(token)]
        if invalid:
            problems.append(
                CodeownersProblem(
                    line=index,
                    message=(
                        "Owners must be @user, @org/team, or an email address; "
                        "rejected " + ", ".join(repr(token) for token in invalid) + "."
                    ),
                )
            )
            continue
        # A rule with no owners is legal: it un-assigns ownership for the pattern.
        rules.append(
            CodeownersRule(line=index, pattern=pattern, owners=tuple(owners))
        )
    return CodeownersRuleset(
        path=path,
        rules=tuple(rules),
        problems=tuple(problems),
    )


@dataclass(frozen=True, slots=True)
class CodeownersDiscovery:
    """What the discovery order found, and why it stopped there."""

    path: str | None = None
    ruleset: CodeownersRuleset | None = None
    error: str | None = None

    @property
    def resolved(self) -> bool:
        return self.ruleset is not None


def discover_codeowners(repository: Path) -> CodeownersDiscovery:
    """Find and parse the effective CODEOWNERS file, or explain the failure.

    Discovery stops at the first candidate that exists. A candidate that exists
    but cannot be trusted -- a symlink, a directory, an unreadable file -- is a
    failure, not a reason to fall through to a lower-precedence location.
    """

    for candidate in CODEOWNERS_LOCATIONS:
        try:
            location = safe_repository_path(repository, candidate)
        except RCPError:
            return CodeownersDiscovery(
                path=candidate,
                error=(
                    f"{candidate} cannot be read safely because its path contains a "
                    "symbolic link."
                ),
            )
        if location.is_symlink():
            return CodeownersDiscovery(
                path=candidate,
                error=(
                    f"{candidate} is a symbolic link; replace it with a regular file "
                    "so ownership cannot be redirected."
                ),
            )
        if not location.exists():
            continue
        if not location.is_file():
            return CodeownersDiscovery(
                path=candidate,
                error=f"{candidate} exists but is not a regular file.",
            )
        try:
            size = location.stat().st_size
        except OSError as error:
            return CodeownersDiscovery(
                path=candidate,
                error=f"{candidate} cannot be inspected: {type(error).__name__}.",
            )
        if size >= CODEOWNERS_MAX_BYTES:
            # GitHub loads no owners at all from an oversized file, so falling
            # through to a lower-precedence location would resolve owners the
            # repository does not actually have.
            return CodeownersDiscovery(
                path=candidate,
                error=(
                    f"{candidate} is {size} bytes; GitHub ignores a CODEOWNERS file "
                    f"of {CODEOWNERS_MAX_BYTES} bytes or more, so no owner would be "
                    "assigned. Split the rules or shorten the file."
                ),
            )
        try:
            text = location.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            return CodeownersDiscovery(
                path=candidate,
                error=f"{candidate} cannot be read: {type(error).__name__}.",
            )
        return CodeownersDiscovery(
            path=candidate,
            ruleset=parse_codeowners(text, path=candidate),
        )
    return CodeownersDiscovery()
