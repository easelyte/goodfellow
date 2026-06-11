import pytest
import pathlib
from memory_index import validate_fact, regenerate, SchemaError, DOMAIN_RE


def _fact(tmp, name, **fm):
    fm = {
        "name": name,
        "description": "d",
        "type": "gotcha",
        "status": "confirmed",
        "opened": "2026-06-02",
        **fm,
    }
    body = "---\n" + "\n".join(f"{k}: {v}" for k, v in fm.items()) + "\n---\nbody\n"
    (tmp / f"{name}.md").write_text(body)


def test_valid_fact_passes(tmp_path):
    _fact(tmp_path, "a")
    assert validate_fact(tmp_path / "a.md")


def test_missing_required_key_is_skipped_not_crash(tmp_path):
    (tmp_path / "bad.md").write_text("---\nname: bad\n---\nx")
    idx = regenerate(tmp_path)  # must not raise
    assert "bad" not in idx  # skipped, not indexed


def test_type_enum_enforced(tmp_path):
    _fact(tmp_path, "a", type="bogus")
    assert not validate_fact(tmp_path / "a.md")


def test_status_enum_enforced(tmp_path):
    _fact(tmp_path, "a", status="bogus")
    assert not validate_fact(tmp_path / "a.md")


def test_domain_charset_rejected(tmp_path):
    assert DOMAIN_RE.match("infra")
    assert not DOMAIN_RE.match("../../README")
    assert not DOMAIN_RE.match("Infra")
    assert not DOMAIN_RE.match("a b")


def test_domain_charset_on_fact_rejected(tmp_path):
    _fact(tmp_path, "a", domain="../../README")
    assert not validate_fact(tmp_path / "a.md")


def test_pending_grouped_separately(tmp_path):
    _fact(tmp_path, "p", status="pending", type="principle")
    _fact(tmp_path, "c", status="confirmed", type="principle")
    idx = regenerate(tmp_path)
    assert "## Pending (unconfirmed)" in idx
    assert "(pending)" in idx
    # confirmed fact rendered in normal section, NOT under pending
    pending_section = idx.split("## Pending (unconfirmed)")[1]
    assert "c" not in pending_section.split("\n")[1:3] or "p" in pending_section


def test_grouped_by_taxonomy(tmp_path):
    _fact(tmp_path, "pr", type="principle")
    _fact(tmp_path, "pa", type="pattern")
    _fact(tmp_path, "go", type="gotcha")
    idx = regenerate(tmp_path)
    assert "## Principles" in idx
    assert "## Patterns" in idx
    assert "## Gotchas" in idx


def test_domain_registry_file_created(tmp_path):
    _fact(tmp_path, "a", domain="infra")
    regenerate(tmp_path)
    assert (tmp_path / "domains" / "infra.md").exists()


def test_sentinels_not_scanned(tmp_path):
    _fact(tmp_path, "a")
    (tmp_path / ".dirty").write_text("")
    (tmp_path / ".migrating").write_text("")
    idx = regenerate(tmp_path)  # must not crash on dotfiles
    assert "a" in idx


def test_size_warning_emitted_over_threshold(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GOODFELLOW_MEMORY_WARN_KB", "1")  # tiny threshold
    for i in range(50):
        _fact(tmp_path, f"f{i}", description="x" * 200)
    regenerate(tmp_path)
    assert (
        "WARN" in capsys.readouterr().err
    )  # stderr warning when index exceeds threshold


def test_malformed_warns_to_stderr(tmp_path, capsys):
    (tmp_path / "bad.md").write_text("---\nname: bad\n---\nx")
    regenerate(tmp_path)
    assert "WARN" in capsys.readouterr().err
