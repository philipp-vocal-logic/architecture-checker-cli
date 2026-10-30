"""Atomic unit and integration tests for archcheck."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from archcheck import Rule, check_rule, check_text, main, resolve_files

# --- Layer 1: Pure Constraint Checking (check_text) ---


def test_check_text_passes_when_under_max_lines() -> None:
    assert check_text("src/a.py", "x = 1\ny = 2\n", max_lines=2) == []


def test_check_text_reports_max_lines_finding_without_line_number() -> None:
    findings = check_text("src/a.py", "a\nb\nc\n", max_lines=2)
    assert findings == ["src/a.py: 3/2 lines"]


def test_check_text_passes_when_no_forbid_pattern_matches() -> None:
    findings = check_text(
        "src/domain/order.py", "from src.domain import port\n", forbid=[re.compile(r"infra")]
    )
    assert findings == []


def test_check_text_reports_forbid_match_with_exact_line_number() -> None:
    text = "import dataclasses\nfrom app.infra.db import Session\n"
    findings = check_text(
        "src/domain/order.py", text, forbid=[re.compile(r"^\s*from\s+app\.infra")]
    )
    assert findings == ["src/domain/order.py:2: from app.infra.db import Session"]


def test_check_text_reports_multiple_forbid_matches_in_order() -> None:
    text = "import os\nx = os.environ['KEY']\nfrom ..infra import db\n"
    findings = check_text(
        "src/domain/order.py",
        text,
        forbid=[re.compile(r"\bos\.environ\b"), re.compile(r"^\s*from\s+\.\.")],
    )
    assert findings == [
        "src/domain/order.py:2: x = os.environ['KEY']",
        "src/domain/order.py:3: from ..infra import db",
    ]


def test_check_text_deduplicates_line_matching_multiple_forbid_patterns() -> None:
    text = "from app.infra import sqlalchemy\n"
    findings = check_text(
        "src/a.py", text, forbid=[re.compile(r"infra"), re.compile(r"sqlalchemy")]
    )
    assert findings == ["src/a.py:1: from app.infra import sqlalchemy"]


def test_check_text_splits_on_newline_only() -> None:
    findings = check_text(
        "src/a.py", "a\x0cb\nfrom app import x\n", max_lines=2, forbid=[re.compile(r"app")]
    )
    assert findings == ["src/a.py:2: from app import x"]


# --- Layer 2: Filesystem Resolution (resolve_files) ---


def test_resolve_files_matches_glob_patterns(tmp_path: Path) -> None:
    domain_file = tmp_path / "src" / "domain" / "order.py"
    domain_file.parent.mkdir(parents=True)
    domain_file.write_text("x = 1\n", encoding="utf-8")
    assert resolve_files(tmp_path, ["src/domain/**/*.py"]) == [domain_file.resolve()]


def test_resolve_files_excludes_ignored_patterns(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    keep, ignored = src / "order.py", src / "test_order.py"
    keep.write_text("x = 1\n", encoding="utf-8")
    ignored.write_text("x = 1\n", encoding="utf-8")
    assert resolve_files(tmp_path, ["src/*.py"], ignore=["src/test_*.py"]) == [keep.resolve()]


def test_resolve_files_excludes_ignored_directory(tmp_path: Path) -> None:
    legacy = tmp_path / "src" / "legacy"
    legacy.mkdir(parents=True)
    keep, old = tmp_path / "src" / "app.py", legacy / "old.py"
    keep.write_text("x = 1\n", encoding="utf-8")
    old.write_text("x = 1\n", encoding="utf-8")
    assert resolve_files(tmp_path, ["src/**/*.py"], ignore=["src/legacy"]) == [keep.resolve()]


def test_resolve_files_filters_to_explicit_target_files(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    first, second = src / "a.py", src / "b.py"
    first.write_text("x = 1\n", encoding="utf-8")
    second.write_text("y = 2\n", encoding="utf-8")
    assert resolve_files(tmp_path, ["src/*.py"], targets={second}) == [second]


def test_resolve_files_filters_to_target_directory(tmp_path: Path) -> None:
    domain, infra = tmp_path / "src" / "domain", tmp_path / "src" / "infra"
    domain.mkdir(parents=True)
    infra.mkdir(parents=True)
    domain_file = domain / "order.py"
    domain_file.write_text("x = 1\n", encoding="utf-8")
    (infra / "db.py").write_text("y = 2\n", encoding="utf-8")
    result = resolve_files(tmp_path, ["src/**/*.py"], targets={domain})
    assert result == [domain_file]


def test_resolve_files_ignores_files_below_ignored_directory_glob(tmp_path: Path) -> None:
    vendored = tmp_path / "vendor" / "lib"
    vendored.mkdir(parents=True)
    for index in range(50):
        (vendored / f"m{index}.py").write_text("", encoding="utf-8")
    keep = tmp_path / "app.py"
    keep.write_text("", encoding="utf-8")
    assert resolve_files(tmp_path, ["**/*.py"], ignore=["vendor/**"]) == [keep]


# --- Layer 3: Rule Orchestration & CLI Zero-Noise Contract (check_rule, main) ---


def test_check_rule_appends_fix_once_for_multiple_findings(tmp_path: Path) -> None:
    (tmp_path / "order.py").write_text("import infra_a\nimport infra_b\n", encoding="utf-8")
    rule = {"files": ["*.py"], "forbid": [r"^import infra"], "fix": "Use domain ports instead."}
    assert check_rule(tmp_path, Rule.from_toml(rule)) == [
        "order.py:1: import infra_a",
        "order.py:2: import infra_b",
        "fix: Use domain ports instead.",
    ]


def test_check_rule_returns_empty_list_when_no_findings(tmp_path: Path) -> None:
    (tmp_path / "order.py").write_text("x = 1\n", encoding="utf-8")
    rule = {"files": ["*.py"], "max_lines": 10, "fix": "Split module."}
    assert check_rule(tmp_path, Rule.from_toml(rule)) == []


def test_check_rule_reports_forbidden_files_when_only_globs_configured(tmp_path: Path) -> None:
    (tmp_path / "test_order.py").write_text("", encoding="utf-8")
    (tmp_path / "order_test.py").write_text("", encoding="utf-8")
    rule = {"files": ["*.py"], "ignore": ["test_*.py"], "fix": "Use test_ prefix."}
    assert check_rule(tmp_path, Rule.from_toml(rule)) == [
        "order_test.py: forbidden file",
        "fix: Use test_ prefix.",
    ]


@pytest.mark.parametrize(
    "bad_rule",
    [
        {"files": ["*.py"]},
        {"files": ["*.py"], "max_line": 5, "fix": "Typo key."},
        {"files": "*.py", "fix": "String instead of list."},
        {"files": ["*.py"], "max_lines": True, "fix": "Bool instead of int."},
        {"files": ["*.py"], "max_lines": 0, "fix": "Zero limit."},
        {"files": ["*.py"], "forbid": [], "fix": "Empty forbid."},
        {"files": ["/tmp/*.py"], "max_lines": 5, "fix": "Absolute glob."},
        {"files": ["../*.py"], "max_lines": 5, "fix": "Glob escapes root."},
        {"files": ["*.py"], "forbid": ["("], "fix": "Broken regex."},
    ],
)
def test_rule_from_toml_rejects_bad_config(bad_rule: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Rule.from_toml(bad_rule)


def test_rule_from_toml_error_names_unknown_key() -> None:
    with pytest.raises(ValueError, match="unknown keys: max_line"):
        Rule.from_toml({"files": ["*.py"], "max_line": 5, "fix": "Typo key."})


def test_check_rule_rejects_content_glob_matching_nothing(tmp_path: Path) -> None:
    rule = {"files": ["scr/**/*.py"], "max_lines": 5, "fix": "Split module."}
    with pytest.raises(ValueError, match="matches nothing: 'scr/"):
        check_rule(tmp_path, Rule.from_toml(rule))


def test_check_rule_allows_forbidden_file_glob_matching_nothing(tmp_path: Path) -> None:
    assert (
        check_rule(tmp_path, Rule.from_toml({"files": ["**/utils.py"], "fix": "Name modules."}))
        == []
    )


def test_check_rule_error_names_non_utf8_file(tmp_path: Path) -> None:
    (tmp_path / "bin.py").write_bytes(b"\xff\xfe\n")
    with pytest.raises(ValueError, match="bin.py: not valid UTF-8"):
        check_rule(
            tmp_path, Rule.from_toml({"files": ["*.py"], "max_lines": 5, "fix": "Use UTF-8."})
        )


def test_check_rule_reports_symlink_pointing_outside_root(tmp_path: Path) -> None:
    outside, root = tmp_path / "outside.py", tmp_path / "repo"
    outside.write_text("a\nb\n", encoding="utf-8")
    root.mkdir()
    (root / "link.py").symlink_to(outside)
    rule = {"files": ["*.py"], "max_lines": 1, "fix": "Split module."}
    assert check_rule(root, Rule.from_toml(rule)) == ["link.py: 2/1 lines", "fix: Split module."]


def test_main_returns_zero_and_emits_no_output_on_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    config = tmp_path / "arch.toml"
    config.write_text('[[rules]]\nfiles = ["*.py"]\nmax_lines = 10\nfix = "Keep small."\n')
    assert main(["--config", str(config)]) == 0
    assert capsys.readouterr() == ("", "")


def test_main_returns_one_and_writes_findings_to_stderr_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "app.py").write_text("import secret_db\n", encoding="utf-8")
    config = tmp_path / "arch.toml"
    config.write_text(
        '[[rules]]\nfiles = ["*.py"]\nforbid = ["secret_db"]\nfix = "Use repository port."\n'
    )
    assert main(["--config", str(config)]) == 1
    assert capsys.readouterr() == ("", "app.py:1: import secret_db\nfix: Use repository port.\n")


def test_main_returns_two_on_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "arch.toml"
    config.write_text('[[rules]]\nfiles = ["*.py"]\nforbid = ["x"]\nfix = "Fix."\n')
    assert main(["--config", str(tmp_path / "missing.toml")]) == 2
    assert main(["nosuch.py", "--config", str(config)]) == 2
    config.write_text('[[rules]]\nfiles = ["*.py"]\nforbid = ["("]\nfix = "Fix."\n')
    assert main(["--config", str(config)]) == 2
    config.write_text("")
    assert main(["--config", str(config)]) == 2
    assert "error:" in capsys.readouterr().err


def test_main_error_is_prefixed_with_rule_number(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "arch.toml"
    config.write_text('[[rules]]\nfiles = ["*.py"]\nfix = "Ok."\n\n[[rules]]\nfiles = ["*.py"]\n')
    assert main(["--config", str(config)]) == 2
    assert capsys.readouterr().err == "error: rule 2: missing keys: fix\n"


def test_main_rejects_target_outside_config_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    config = root / "arch.toml"
    config.write_text('[[rules]]\nfiles = ["*.py"]\nfix = "Fix."\n')
    assert main([str(tmp_path), "--config", str(config)]) == 2
    assert "target outside config directory" in capsys.readouterr().err


def test_main_filters_to_explicit_target_directory_relative_to_cwd(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    src_dir, tests_dir = tmp_path / "src", tmp_path / "tests"
    src_dir.mkdir()
    tests_dir.mkdir()
    (src_dir / "app.py").write_text("import secret_db\n", encoding="utf-8")
    (tests_dir / "test_app.py").write_text("x = 1\n", encoding="utf-8")
    config = tmp_path / "arch.toml"
    config.write_text('[[rules]]\nfiles = ["**/*.py"]\nforbid = ["secret_db"]\nfix = "Use port."\n')
    assert main(["tests/", "--config", str(config)]) == 0
    assert capsys.readouterr().err == ""
    assert main(["src/", "--config", str(config)]) == 1
    assert "src/app.py:1: import secret_db" in capsys.readouterr().err


def test_project_complies_with_own_arch_toml() -> None:
    repo_config = Path(__file__).resolve().parent.parent / "arch.toml"
    assert main(["--config", str(repo_config)]) == 0
