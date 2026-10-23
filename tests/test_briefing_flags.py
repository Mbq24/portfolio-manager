"""Boolean env flags must accept the spellings people actually type.

Why this exists: PM_BRIEFING_ENABLED was tested as
`os.environ.get("PM_BRIEFING_ENABLED", "1") == "1"`. Setting it to `true` — the obvious
spelling, and what a dashboard suggests — left the daily briefing silently disabled for
days. The dashboard showed the variable set; /health reported enabled:false; nothing
logged a complaint. A one-string difference cost a full debugging cycle.

Two rules are pinned here:
  1. The common truthy/falsy spellings all work, case-insensitively, whitespace-trimmed.
  2. An unparseable value falls back to the DEFAULT LOUDLY. A feature silently
     switching itself off is the failure mode that hurts; a noisy-but-running briefing
     beats a silent one.
"""
import pytest

from briefing import briefing_enabled, env_flag

NAME = "PM_BRIEFING_ENABLED"


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "True", "yes", "YES", "on",
                                 "enabled", "t", "y", " true ", " 1\n"])
def test_truthy_spellings_enable(raw, monkeypatch):
    monkeypatch.setenv(NAME, raw)
    assert env_flag(NAME) is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "False", "off", "no",
                                 "disabled", "f", "n", " false "])
def test_falsy_spellings_disable(raw, monkeypatch):
    monkeypatch.setenv(NAME, raw)
    assert env_flag(NAME) is False


def test_absent_variable_uses_the_default(monkeypatch):
    monkeypatch.delenv(NAME, raising=False)
    assert env_flag(NAME, True) is True
    assert env_flag(NAME, False) is False


def test_empty_value_uses_the_default(monkeypatch):
    monkeypatch.setenv(NAME, "")
    assert env_flag(NAME, True) is True
    assert env_flag(NAME, False) is False


def test_unparseable_value_falls_back_to_default_and_says_so(monkeypatch, capsys):
    monkeypatch.setenv(NAME, "banana")
    assert env_flag(NAME, True) is True
    out = capsys.readouterr().out
    assert "banana" in out
    assert "not a recognised boolean" in out


def test_briefing_enabled_reads_the_real_flag(monkeypatch):
    monkeypatch.setenv(NAME, "true")
    assert briefing_enabled() is True
    monkeypatch.setenv(NAME, "1")
    assert briefing_enabled() is True
    monkeypatch.setenv(NAME, "0")
    assert briefing_enabled() is False
