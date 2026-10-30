"""Minimal zero-noise architecture checker CLI for agentic coding tools."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

RULE_KEYS = frozenset({"files", "fix", "ignore", "max_lines", "forbid"})


def _is_str_seq(seq: Any) -> bool:
    return isinstance(seq, (list, tuple)) and all(isinstance(s, str) for s in seq)


def _check_rule_config(rule: Mapping[str, Any]) -> None:
    if not isinstance(rule, Mapping) or not {"files", "fix"} <= set(rule) <= RULE_KEYS:
        raise ValueError("Invalid rule keys")
    max_lines, forbid = rule.get("max_lines"), rule.get("forbid", ())
    checks = (
        _is_str_seq(rule["files"]) and bool(rule["files"]),
        isinstance(rule["fix"], str) and bool(rule["fix"]),
        _is_str_seq(rule.get("ignore", ())) and _is_str_seq(forbid),
        isinstance(max_lines, int | None) and not isinstance(max_lines, bool),
    )
    if not all(checks):
        raise ValueError("Invalid rule config")
    for pattern in forbid:
        re.compile(pattern)


def check_text(
    path: str, text: str, max_lines: int | None = None, forbid: Sequence[str] = ()
) -> list[str]:
    """Check file content against line count limits and forbidden regex patterns."""
    lines = text.removesuffix("\n").split("\n") if text else []
    findings: list[str] = []
    if max_lines is not None and len(lines) > max_lines:
        findings.append(f"{path}: {len(lines)}/{max_lines} lines")
    compiled = [re.compile(pattern) for pattern in forbid]
    for line_no, line in enumerate(lines, start=1):
        if any(regex.search(line) for regex in compiled):
            findings.append(f"{path}:{line_no}: {line.strip()}")
    return findings


def _resolve_target(root: Path, target: Path) -> Path:
    resolved = root / target if not target.is_absolute() and (root / target).exists() else target
    if not resolved.exists():
        raise FileNotFoundError(f"Target not found: {target}")
    return resolved.resolve()


def _within(path: Path, roots: set[Path]) -> bool:
    return any(path.is_relative_to(r) for r in roots)


def resolve_files(
    root: Path, files: Sequence[str], ignore: Sequence[str] = (), targets: set[Path] | None = None
) -> list[Path]:
    """Resolve files matching `files` globs minus `ignore` globs."""
    matched = {p.resolve() for pattern in files for p in root.glob(pattern) if p.is_file()}
    ignored = {p.resolve() for pattern in ignore for p in root.glob(pattern)}
    selected = {p for p in matched if not _within(p, ignored)}
    if targets is not None:
        resolved = {_resolve_target(root, t) for t in targets}
        selected = {p for p in selected if _within(p, resolved)}
    return sorted(selected)


def check_rule(root: Path, rule: Mapping[str, Any], targets: set[Path] | None = None) -> list[str]:
    """Check a single architecture rule across all matching files."""
    _check_rule_config(rule)
    resolved_root = root.resolve()
    paths = resolve_files(resolved_root, rule["files"], rule.get("ignore", ()), targets)
    max_lines, forbid = rule.get("max_lines"), rule.get("forbid", ())
    findings: list[str] = []
    for path in paths:
        rel = path.relative_to(resolved_root).as_posix()
        if max_lines is None and not forbid:
            findings.append(f"{rel}: forbidden file")
        else:
            findings.extend(check_text(rel, path.read_text(encoding="utf-8"), max_lines, forbid))
    return [*findings, f"fix: {rule['fix']}"] if findings else []


def _load_rules(config_path: Path) -> Sequence[Mapping[str, Any]]:
    with config_path.open("rb") as f:
        config = tomllib.load(f)
    if set(config) - {"rules"} or not isinstance(config.get("rules", []), list):
        raise ValueError("Invalid config")
    return config.get("rules", [])


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: returns 0 on pass, 1 on findings, 2 on error."""
    parser = argparse.ArgumentParser(prog="architecture")
    parser.add_argument("targets", nargs="*", type=Path)
    parser.add_argument("--config", type=Path, default=Path("arch.toml"))
    args = parser.parse_args(argv)
    root = args.config.resolve().parent
    try:
        rules = _load_rules(args.config.resolve())
        targets = {_resolve_target(root, t) for t in args.targets} if args.targets else None
        output = [line for rule in rules for line in check_rule(root, rule, targets)]
    except (OSError, tomllib.TOMLDecodeError, ValueError, re.error) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if output:
        print(*output, sep="\n", file=sys.stderr)
    return 1 if output else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
