"""Static checks on the GitHub Actions workflows (CLAUDE.md §18): every action
pinned to a commit SHA with its tag in a comment, a read-only default token,
and the reference-pack workflow's narrower rules — only `publish` may write,
the Finnhub secret reaches the build step only, and nothing ever commits.

Text-based on purpose: PyYAML is not a dependency."""

import re
from pathlib import Path

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows"
REF = WF / "reference-pack.yml"


def _jobs(text: str) -> dict[str, str]:
    """{job id: its block} for the top-level `jobs:` mapping."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([a-z][a-z0-9-]*):\n", body)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def test_every_action_is_sha_pinned_with_its_tag():
    for wf in WF.glob("*.yml"):
        for line in wf.read_text().splitlines():
            m = re.search(r"uses:\s*(\S+)", line)
            if not m or m.group(1).startswith("./"):
                continue
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", m.group(1)), (wf.name, line)
            assert re.search(r"#\s*v\d", line), (wf.name, line)


def test_default_token_is_read_only_everywhere():
    for wf in WF.glob("*.yml"):
        text = wf.read_text()
        assert re.search(r"(?m)^permissions:\n  contents: read$", text), wf.name
        assert not re.search(r"(?m)^\s+pull_request_target:", text), wf.name


def test_reference_pack_only_publish_writes():
    jobs = _jobs(REF.read_text())
    assert set(jobs) == {"yahoo-check", "build", "publish"}
    for name, block in jobs.items():
        writes = "contents: write" in block
        assert writes == (name == "publish"), name


def test_reference_pack_secret_only_in_the_build_step():
    text = REF.read_text()
    jobs = _jobs(text)
    assert text.count("secrets.") == 1
    assert "secrets.FINNHUB_API_KEY" in jobs["build"]
    assert "secrets" not in jobs["publish"] and "secrets" not in jobs["yahoo-check"]
    assert not re.search(r"echo[^\n]*FINNHUB", text)


def test_reference_pack_never_commits_or_creates_the_release():
    text = REF.read_text()
    for bad in ("git commit", "git push", "gh release create", "git tag"):
        assert bad not in text, bad
    assert "gh release upload reference-pack" in text and "--clobber" in text
    assert "uv sync --locked" in text


def test_reference_pack_schedule_and_manual_modes():
    text = REF.read_text()
    assert re.search(r'cron: "\d+ 2[0-3] \* \* 1-5"', text)  # weekdays, after the US close
    assert "workflow_dispatch:" in text
    jobs = _jobs(text)
    assert "--returns-only" in jobs["yahoo-check"]
    assert "--out pack --previous prev" in jobs["build"]


def test_reference_pack_runs_only_on_schedule_or_dispatch():
    """A collector never runs on push or pull_request: the builder spends the
    Finnhub secret and the publish job writes the release. (A temporary push
    trigger existed on `distribution` until dispatch was confirmed to work
    from a branch through the CLI.)"""
    text = REF.read_text()
    on = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    triggers = set(re.findall(r"(?m)^  ([a-z_]+):", on))
    assert triggers == {"schedule", "workflow_dispatch"}, triggers
    jobs = _jobs(text)
    assert "inputs.mode == 'yahoo-check'" in jobs["yahoo-check"]
    assert "github.event_name == 'schedule'" in jobs["build"]
    assert "if:" not in jobs["publish"].split("steps:")[0]
    assert "needs: build" in jobs["publish"]


def test_only_a_missing_release_starts_a_fresh_history():
    """A transient `gh` failure while fetching the previous pack must fail the
    run: treating it as "no previous pack" would publish one day of history
    over up to 400."""
    step = _jobs(REF.read_text())["build"].split("- name: Fetch the previous pack", 1)[1]
    step = step.split("- name:", 1)[0]
    assert 'grep -q "release not found"' in step
    assert "exit 1" in step
    assert "rm -rf prev" not in step
    # the download itself is not wrapped in an `if`: its failure fails the step
    assert "\n          gh release download reference-pack" in step
