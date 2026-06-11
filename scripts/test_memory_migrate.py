import pathlib
import subprocess
import sys
from memory_index import migrate, _parse_frontmatter


def _seed_dirs(tmp):  # MN-1
    gf = tmp / ".goodfellow"
    (gf / "memory").mkdir(parents=True)
    return gf


def _km(gf, text):
    (gf / "knowledge.md").write_text(text)


def _facts(gf):
    return sorted(p for p in (gf / "memory").glob("*.md") if not p.name.startswith("."))


def _only_fact(gf):
    return _parse_frontmatter(_facts(gf)[0])


def test_type_and_opened_inference(tmp_path):
    gf = _seed_dirs(tmp_path)
    _km(gf, "## Principles\n- 2026-06-02: always validate at boundaries\n")
    migrate(gf)
    f = _only_fact(gf)
    assert f["type"] == "principle" and f["opened"] == "2026-06-02"


def test_pattern_and_gotcha_inference(tmp_path):
    gf = _seed_dirs(tmp_path)
    _km(
        gf,
        "## Patterns\n- 2026-06-02: convergence termination\n## Gotchas\n- 2026-06-03: null not undefined\n",
    )
    migrate(gf)
    types = sorted(_parse_frontmatter(p)["type"] for p in _facts(gf))
    assert types == ["gotcha", "pattern"]


def test_status_from_pending_tag(tmp_path):
    gf = _seed_dirs(tmp_path)
    _km(gf, "## Gotchas\n- [pending] 2026-06-02: a footgun\n")
    migrate(gf)
    assert _only_fact(gf)["status"] == "pending"


def test_headerless_defaults_principle_and_reported(tmp_path):
    gf = _seed_dirs(tmp_path)
    _km(gf, "- 2026-06-02: some prose\n")
    migrate(gf)
    assert _only_fact(gf)["type"] == "principle"
    assert (gf / "migration-report.md").exists()  # outside fact glob (gf root)
    assert not (gf / "memory" / "migration-report.md").exists()


def test_undated_entry_gets_run_date(tmp_path):
    gf = _seed_dirs(tmp_path)
    _km(gf, "## Gotchas\n- no date here\n")
    migrate(gf)
    import re

    assert re.match(r"\d{4}-\d{2}-\d{2}", _only_fact(gf)["opened"])


def test_duplicate_body_both_migrate(tmp_path):
    gf = _seed_dirs(tmp_path)
    _km(
        gf,
        "## Patterns\n- 2026-06-02: cache invalidation\n## Gotchas\n- 2026-06-03: cache invalidation\n",
    )
    migrate(gf)
    assert len(_facts(gf)) == 2  # foo, foo-2 — none dropped


def test_idempotent_rerun_is_noop(tmp_path):
    gf = _seed_dirs(tmp_path)
    _km(gf, "## Gotchas\n- 2026-06-02: a\n- 2026-06-03: b\n")
    migrate(gf)
    n1 = len(_facts(gf))
    migrate(gf)  # re-run over same knowledge.md
    n2 = len(_facts(gf))
    assert n1 == n2 == 2


def test_crash_resume_via_migrating_sentinel(tmp_path):
    gf = _seed_dirs(tmp_path)
    _km(gf, "## Gotchas\n- 2026-06-02: a\n- 2026-06-03: b\n")
    (gf / "memory" / ".migrating").write_text("")
    migrate(gf)  # resumes, source-identity key skips written, completes
    assert not (gf / "memory" / ".migrating").exists()
    assert len(_facts(gf)) == 2


def test_irregular_lands_in_principles_section(tmp_path):
    gf = _seed_dirs(tmp_path)
    _km(gf, "- 2026-06-02: uncategorized prose rule\n")
    migrate(gf)
    idx = (gf / "MEMORY.md").read_text()
    assert "## Principles" in idx
    assert "uncategorized prose rule" in idx


def test_knowledge_left_in_place(tmp_path):
    gf = _seed_dirs(tmp_path)
    _km(gf, "## Gotchas\n- 2026-06-02: a\n")
    migrate(gf)
    assert (gf / "knowledge.md").exists()  # non-destructive backup


def test_auto_migrate_under_write_lock_no_deadlock(tmp_path):
    gf = tmp_path / ".goodfellow"
    (gf / "memory").mkdir(parents=True)
    (gf / "knowledge.md").write_text("## Gotchas\n- 2026-06-02: legacy fact\n")
    # write_fact must auto-migrate (knowledge.md non-empty, memory/ empty) WITHOUT deadlock
    r = subprocess.run(
        [
            sys.executable,
            "memory_index.py",
            "--root",
            str(gf),
            "write-fact",
            "--name",
            "new",
            "--description",
            "d",
            "--type",
            "gotcha",
            "--status",
            "pending",
            "--opened",
            "2026-06-10",
            "--body",
            "x",
        ],
        cwd=pathlib.Path(__file__).parent,
        timeout=20,  # timeout catches a deadlock
    )
    assert r.returncode == 0
    idx = (gf / "MEMORY.md").read_text()
    stems = [p.stem for p in (gf / "memory").glob("*.md") if not p.name.startswith(".")]
    assert "legacy fact" in idx or "legacy-fact" in stems
    assert "new" in stems  # the triggering write also landed


def test_cli_migrate(tmp_path):
    gf = tmp_path / ".goodfellow"
    (gf / "memory").mkdir(parents=True)
    (gf / "knowledge.md").write_text("## Gotchas\n- 2026-06-02: cli legacy\n")
    r = subprocess.run(
        [sys.executable, "memory_index.py", "--root", str(gf), "migrate"],
        cwd=pathlib.Path(__file__).parent,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0
    assert any(not p.name.startswith(".") for p in (gf / "memory").glob("*.md"))
    # report path printed to stderr
    assert "migration-report.md" in r.stderr
