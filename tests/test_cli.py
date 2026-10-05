"""CLI smoke tests (``TASK_PLAN.md`` §M0-11).

``python -m app.cli import-legacy`` is the documented migration command, so it is
exercised end-to-end against a temporary SQLite database with a fake panel (the
import itself is covered in ``tests/test_legacy_import.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import cli
from app.services.subscriptions import now_ms
from app.settings import Settings
from tests.fakes import FakePanel

USER = 1001


def test_import_legacy_subcommand_parses() -> None:
    """AC: ``import-legacy --dry-run`` is accepted by the parser."""
    args = cli.build_parser().parse_args(["import-legacy", "--dry-run"])
    assert args.command == "import-legacy"
    assert args.dry_run is True


def test_import_legacy_runs_for_real_by_default() -> None:
    args = cli.build_parser().parse_args(["import-legacy"])
    assert args.dry_run is False


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


async def test_import_legacy_creates_a_database_and_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC: the CLI migrates the DB, imports the panel and pins placeholders."""
    settings = Settings(
        _env_file=None,
        bot_token="123456:AA-test",
        vpn_token="vpn-token-value",
        domain="https://panel.example.com/",
        sub_url_base="https://panel.example.com/sub/",
        inbound_id=1,
        owner_ids=[1],
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}",
        banned_users_file=str(tmp_path / "banned_users.json"),
    )
    panel = FakePanel()
    panel.seed(USER, expiry_ms=0, enable=False)  # legacy placeholder
    monkeypatch.setattr(cli, "PanelGateway", lambda _settings: panel)

    code = await cli.import_legacy(settings, dry_run=False)

    assert code == cli.EXIT_OK
    assert (tmp_path / "bot.db").exists()
    assert "users created: 1" in capsys.readouterr().out
    client = await panel.get_client(USER)
    assert client is not None
    assert 0 < int(client.expiry_time) <= now_ms() + 1000
