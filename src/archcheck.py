"""Minimal zero-noise architecture checker CLI for agentic coding tools."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

RULE_KEYS = {"files", "fix", "ignore", "max_lines", "forbid"}


def _check_keys(table: Any) -> None:
    if not isinstance(table, Mapping):
        raise ValueError("rule must be a table")
    if unknown := set(table) - RULE_KEYS:
        raise ValueError(f"unknown keys: {', '.join(sorted(unknown))}")
    if missing := {"files", "fix"} - set(table):
        raise ValueError(f"missing keys: {', '.join(sorted(missing))}")


def _str_list(table: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """Return an optional non-empty list of strings as a tuple (empty when absent)."""
    if key not in table:
        return ()
    value = table[key]
    if not isinstance(value, list) or not value or not all(isinstance(s, str) for s in value):
        raise ValueError(f"{key} must be a non-empty list of strings")
    return tuple(value)


def _globs(table: Mapping[str, Any], key: str) -> tuple[str, ...]:
    patterns = _str_list(table, key)
    for pattern in patterns:
        path = PurePosixPath(pattern)
        if not pattern or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"glob must be relative to the config directory: {pattern!r}")
    return patterns


def _regexes(table: Mapping[str, Any]) -> tuple[re.Pattern[str], ...]:
    compiled = []
    for pattern in _str_list(table, "forbid"):
        try:
            compiled.append(re.compile(pattern))
        except re.error as error:
            raise ValueError(f"invalid forbid regex {pattern!r}: {error}") from None
    return tuple(compiled)


def _max_lines(table: Mapping[str, Any]) -> int | None:
    max_lines = table.get("max_lines")
    if max_lines is None:
        return None
    if isinstance(max_lines, bool) or not isinstance(max_lines, int) or max_lines < 1:
        raise ValueError("max_lines must be a positive integer")
    return max_lines


def _fix(table: Mapping[str, Any]) -> str:
    if not isinstance(table["fix"], str) or not table["fix"]:
        raise ValueError("fix must be a non-empty string")
    return table["fix"]


@dataclass(frozen=True)
class Rule:
    """An arch.toml rule: file globs bound to a line limit, forbid regexes, and a fix."""

    files: tuple[str, ...]
    fix: str
    ignore: tuple[str, ...] = ()
    max_lines: int | None = None
    forbid: tuple[re.Pattern[str], ...] = ()

    @property
    def forbids_files(self) -> bool:
        """A rule without content checks forbids every file it matches."""
        return self.max_lines is None and not self.forbid

    @classmethod
    def from_toml(cls, table: Any) -> Rule:
        """Build a rule from an arch.toml table, raising ValueError on the first problem."""
        _check_keys(table)
        return cls(
            files=_globs(table, "files"),
            fix=_fix(table),
            ignore=_globs(table, "ignore"),
            max_lines=_max_lines(table),
            forbid=_regexes(table),
        )


def check_text(
    path: str, text: str, max_lines: int | None = None, forbid: Sequence[re.Pattern[str]] = ()
) -> list[str]:
    """Check file content against line count limits and forbidden regex patterns."""
    # Split on "\n" only: str.splitlines() would also split on form feeds and similar.
    lines = text.removesuffix("\n").split("\n") if text else []
    findings: list[str] = []
    if max_lines is not None and len(lines) > max_lines:
        findings.append(f"{path}: {len(lines)}/{max_lines} lines")
    for line_no, line in enumerate(lines, start=1):
        if any(regex.search(line) for regex in forbid):
            findings.append(f"{path}:{line_no}: {line.strip()}")
    return findings


def _glob_all(root: Path, patterns: Iterable[str]) -> set[Path]:
    return {path for pattern in patterns for path in root.glob(pattern)}


def _is_ignored(path: Path, ignored: set[Path]) -> bool:
    return path in ignored or not ignored.isdisjoint(path.parents)


def resolve_files(
    root: Path, files: Sequence[str], ignore: Sequence[str] = (), targets: set[Path] | None = None
) -> list[Path]:
    """Resolve files matching `files` globs minus `ignore` globs, limited to absolute `targets`."""
    ignored = _glob_all(root, ignore)
    selected = {p for p in _glob_all(root, files) if p.is_file() and not _is_ignored(p, ignored)}
    if targets is not None:
        selected = {p for p in selected if any(p.is_relative_to(t) for t in targets)}
    return sorted(selected)


def _read_text(path: Path, rel: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{rel}: not valid UTF-8") from None


def _check_globs_match(root: Path, rule: Rule) -> None:
    """Content rules must match something; otherwise a typo would pass silently."""
    if rule.forbids_files:
        return
    for pattern in rule.files:
        if next(root.glob(pattern), None) is None:
            raise ValueError(f"files glob matches nothing: {pattern!r}")


def check_rule(root: Path, rule: Rule, targets: set[Path] | None = None) -> list[str]:
    """Check a single architecture rule across all matching files."""
    root = root.resolve()
    _check_globs_match(root, rule)
    findings: list[str] = []
    for path in resolve_files(root, rule.files, rule.ignore, targets):
        rel = path.relative_to(root).as_posix()
        if rule.forbids_files:
            findings.append(f"{rel}: forbidden file")
        else:
            findings.extend(check_text(rel, _read_text(path, rel), rule.max_lines, rule.forbid))
    return [*findings, f"fix: {rule.fix}"] if findings else []


def _load_tables(config_path: Path) -> list[Any]:
    with config_path.open("rb") as f:
        config = tomllib.load(f)
    tables = config.get("rules")
    if set(config) != {"rules"} or not isinstance(tables, list) or not tables:
        raise ValueError(f"{config_path.name} must contain only [[rules]] tables (at least one)")
    return tables


def _resolve_target(root: Path, target: Path) -> Path:
    resolved = target.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"target not found: {target}")
    if not resolved.is_relative_to(root):
        raise ValueError(f"target outside config directory {root}: {target}")
    return resolved


def _check_tables(root: Path, tables: list[Any], targets: set[Path] | None) -> list[str]:
    output: list[str] = []
    for index, table in enumerate(tables, start=1):
        try:
            output.extend(check_rule(root, Rule.from_toml(table), targets))
        except (OSError, ValueError) as error:
            raise ValueError(f"rule {index}: {error}") from None
    return output


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: returns 0 on pass, 1 on findings, 2 on error."""
    parser = argparse.ArgumentParser(prog="architecture")
    parser.add_argument("targets", nargs="*", type=Path, help="paths relative to the current dir")
    parser.add_argument("--config", type=Path, default=Path("arch.toml"))
    args = parser.parse_args(argv)
    try:
        config_path = args.config.resolve()
        root = config_path.parent
        tables = _load_tables(config_path)
        targets = {_resolve_target(root, t) for t in args.targets} if args.targets else None
        output = _check_tables(root, tables, targets)
    except Exception as error:  # any failure must map to exit 2, never to "findings"
        print(f"error: {error}", file=sys.stderr)
        return 2
    if output:
        print(*output, sep="\n", file=sys.stderr)
    return 1 if output else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
