"""Packaging, docs, and offline CI."""

from __future__ import annotations

import json
import re
from pathlib import Path

from errand_agent.evals import run_evals

ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "DESIGN.md"
ASSUMPTIONS = ROOT / "ASSUMPTIONS.md"
EVALS = ROOT / "EVALS.md"
README = ROOT / "README.md"
LICENSE = ROOT / "LICENSE"
CI = ROOT / ".github" / "workflows" / "ci.yml"
TRACE = ROOT / "artifacts" / "traces" / "timeout-after-success.jsonl"

EXCERPT_ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*`([^`]+)`\s*\|")
BASH_BLOCK = re.compile(r"```bash\n(.*?)```", re.DOTALL)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
)
SKIP_DIRS = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    ".errand",
    "__pycache__",
}


def test_readme_runs_and_tests_in_three_commands_or_fewer() -> None:
    readme = README.read_text(encoding="utf-8")
    block = next(
        block
        for block in BASH_BLOCK.findall(readme)
        if "uv sync" in block and "pytest" in block
    )
    commands = [line.strip() for line in block.splitlines() if line.strip()]

    assert len(commands) <= 3
    assert commands[0].startswith("uv sync")
    assert any(command.startswith("uv run errand demo") for command in commands)
    assert any(command.startswith("uv run pytest") for command in commands)


def test_design_note_is_two_pages_and_ends_with_the_100x_analysis() -> None:
    text = DESIGN.read_text(encoding="utf-8")
    headings = re.findall(r"^## (.+)$", text, re.MULTILINE)

    assert len(text.split()) <= 1200
    assert headings[-1].startswith("What breaks first at 100")
    section = text.split(f"## {headings[-1]}", 1)[1]
    assert len(section.split()) >= 120
    assert "100×" in headings[-1]


def test_design_excerpt_matches_the_committed_raw_trace() -> None:
    raw = {
        event["seq"]: event
        for event in (
            json.loads(line)
            for line in TRACE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    rows = [
        (int(match.group(1)), match.group(2))
        for line in DESIGN.read_text(encoding="utf-8").splitlines()
        if (match := EXCERPT_ROW.match(line))
    ]

    assert len(rows) >= 5
    for seq, event_type in rows:
        assert seq in raw, f"DESIGN.md references missing seq {seq}"
        assert raw[seq]["type"] == event_type


def test_assumptions_records_the_locked_readings() -> None:
    text = ASSUMPTIONS.read_text(encoding="utf-8").lower()

    for reading in (
        "user_raj",
        "asia/kolkata",
        "inr",
        "one active errand per conversation",
        "no automatic provider substitution",
        "untrusted quotes",
        "next 7 days",
        "offline",
        "mit",
        "conditional",
    ):
        assert reading in text, reading


def test_mit_license_is_present() -> None:
    text = LICENSE.read_text(encoding="utf-8")

    assert text.startswith("MIT License")
    assert "Permission is hereby granted" in text
    assert "THE SOFTWARE IS PROVIDED \"AS IS\"" in text
    assert "2026" in text


def test_ci_runs_the_tests_and_evals_offline() -> None:
    workflow = CI.read_text(encoding="utf-8")

    assert "pull_request" in workflow
    assert "actions/checkout" in workflow
    assert "uv sync --frozen" in workflow
    assert "uv run pytest" in workflow
    assert "uv run errand evals" in workflow
    assert "git diff --exit-code" in workflow


def test_readme_links_the_project_docs() -> None:
    readme = README.read_text(encoding="utf-8")

    for target in (
        "DESIGN.md",
        "ASSUMPTIONS.md",
        "EVALS.md",
        "BUGS.md",
        "LICENSE",
        "artifacts/traces/",
        ".github/workflows/ci.yml",
    ):
        assert target in readme, target


def test_readme_stays_concise() -> None:
    text = README.read_text(encoding="utf-8")

    assert len(text.splitlines()) <= 160
    assert len(text.split()) <= 1000


def test_readme_has_no_recording_or_publication_content() -> None:
    text = README.read_text(encoding="utf-8").lower()

    for phrase in (
        "demo video",
        "demo recording",
        "unlisted",
        "loom",
        "youtube",
        "runbook",
        "submission",
        "make the repo public",
        "flip the github",
    ):
        assert phrase not in text, phrase


def test_evals_table_matches_the_latest_run(tmp_path) -> None:
    output = tmp_path / "EVALS.md"

    report = run_evals(output_path=output)

    assert report.ok
    assert output.read_text(encoding="utf-8") == EVALS.read_text(encoding="utf-8")


def test_no_secrets_are_committed() -> None:
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in {".pyc", ".woff2"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            assert not pattern.search(text), f"secret-like value in {path.name}"
