"""Minimal zero-noise architecture checker CLI for agentic coding tools."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
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


def _match_parts(path_parts: tuple[str, ...], pattern_parts: tuple[str, ...]) -> bool:
    if not pattern_parts:
        return not path_parts
    if pattern_parts == ("**",):
        return bool(path_parts)
    if pattern_parts[0] == "**":
        return _match_parts(path_parts, pattern_parts[1:]) or (
            bool(path_parts) and _match_parts(path_parts[1:], pattern_parts)
        )
    return (
        bool(path_parts)
        and fnmatchcase(path_parts[0], pattern_parts[0])
        and _match_parts(path_parts[1:], pattern_parts[1:])
    )


def _matches(rel: PurePosixPath, pattern: str) -> bool:
    return _match_parts(rel.parts, PurePosixPath(pattern).parts)


def _is_ignored(rel: PurePosixPath, ignore: Sequence[str]) -> bool:
    return any(_matches(rel, pattern) for pattern in ignore)


def _may_contain(rel_dir: PurePosixPath, pattern: str) -> bool:
    """Whether files below `rel_dir` could match `pattern`."""
    pattern_parts = PurePosixPath(pattern).parts
    for dir_part, pattern_part in zip(rel_dir.parts, pattern_parts, strict=False):
        if pattern_part == "**":
            return True
        if not fnmatchcase(dir_part, pattern_part):
            return False
    return len(rel_dir.parts) < len(pattern_parts)


def _glob(root: Path, files: Sequence[str], ignore: Sequence[str]) -> dict[str, set[Path]]:
    """Map each `files` glob to its matching files in a single walk.

    The walk enters a directory only if it is not ignored and could contain a match.
    Directory symlinks are not followed, so symlink loops cannot occur.
    """
    found: dict[str, set[Path]] = {pattern: set() for pattern in files}
    for dir_path, dir_names, file_names in os.walk(root):
        rel_dir = PurePosixPath(Path(dir_path).relative_to(root).as_posix())
        dir_names[:] = [
            name
            for name in dir_names
            if not _is_ignored(rel_dir / name, ignore)
            and any(_may_contain(rel_dir / name, pattern) for pattern in files)
        ]
        for name in file_names:
            rel = rel_dir / name
            if _is_ignored(rel, ignore) or not (root / rel).is_file():
                continue
            for pattern in files:
                if _matches(rel, pattern):
                    found[pattern].add(root / rel)
    return found


def resolve_files(
    root: Path,
    files: Sequence[str],
    ignore: Sequence[str] = (),
    targets: set[Path] | None = None,
    *,
    require_match: bool = False,
) -> list[Path]:
    """Resolve files matching `files` globs minus `ignore` globs, limited to absolute `targets`.

    With `require_match`, every `files` glob must match a file, so a typo fails loudly.
    """
    found = _glob(root.resolve(), files, ignore)
    if require_match and (unmatched := [pattern for pattern, paths in found.items() if not paths]):
        raise ValueError(f"files glob matches nothing: {unmatched[0]!r}")
    selected = set().union(*found.values())
    if targets is not None:
        selected = {p for p in selected if any(p.is_relative_to(t) for t in targets)}
    return sorted(selected)


def _read_text(path: Path, rel: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{rel}: not valid UTF-8") from None


def check_rule(root: Path, rule: Rule, targets: set[Path] | None = None) -> list[str]:
    """Check a single architecture rule across all matching files."""
    root = root.resolve()
    # Forbidden-file rules are expected to match nothing; content rules must match something.
    require_match = not rule.forbids_files
    paths = resolve_files(root, rule.files, rule.ignore, targets, require_match=require_match)
    findings: list[str] = []
    for path in paths:
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


def _resolve_root(config_path: Path) -> Path:
    resolved = config_path.resolve()
    return (
        resolved.parent.parent
        if resolved.parent.name == ".policy"  # glossary: ignore[policy]
        else resolved.parent
    )


def _resolve_target(root: Path, target: Path) -> Path:
    # Missing targets are allowed: `git diff --name-only` lists deleted files too.
    resolved = target.resolve()
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
        root = _resolve_root(config_path)
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
