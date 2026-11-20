"""Tenant SQLite can add/remove only disposable context without losing history."""

import hashlib
import json
import sqlite3

import pytest
from alembic import command
from sqlalchemy import create_engine

from app.storage import migrations


def original_hash(database):
    with sqlite3.connect(database) as connection:
        values = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("session", "message", "part")
        }
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


@pytest.mark.parametrize("unversioned", [False, True])
def test_v12_projection_upgrade_downgrade_reupgrade_preserves_transcript(tmp_path, monkeypatch, unversioned):
    database = tmp_path / "tenant.sqlite3"
    old_revision = migrations.V110_WORKSPACE_IDENTITY_V2_REVISION
    migrations._run_alembic_revision(database, stamp_revision=None, target_revision=old_revision)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO session (id, slug, directory, title, version) VALUES (?, ?, ?, ?, ?)",
            ("original-session", "original", ".", "Original", "1.1.0"),
        )
        connection.execute("INSERT INTO message (id, session_id, data) VALUES (?, ?, ?)",
                           ("original-message", "original-session", '{"role":"assistant"}'))
        connection.execute("INSERT INTO part (id, message_id, session_id, data) VALUES (?, ?, ?, ?)",
                           ("original-part", "original-message", "original-session",
                            json.dumps({"type": "tool", "tool": "read", "state": {
                                "status": "completed", "input": {}, "output": "full original output" * 100}})))
        connection.commit()
        if unversioned:
            connection.execute("DROP TABLE alembic_version")
            connection.commit()
    before = original_hash(database)

    result = migrations.upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    assert result.previous_revision == (None if unversioned else old_revision)
    assert result.current_revision == migrations.V110_CONTEXT_PROJECTION_REVISION
    assert result.backup_path and result.backup_path.is_file()
    assert original_hash(database) == before
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO session_context_projection "
            "(session_id, anchor_message_id, boundary_text, pruned_tool_part_ids) VALUES (?, ?, ?, ?)",
            ("original-session", "original-message", "model-only boundary", '["original-part"]'),
        )
        connection.commit()

    # A controlled rollback uses the new migration's downgrade before starting
    # the old image. It never restores a backup over newer transcript/ledger data.
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        command.downgrade(migrations._alembic_config(connection), old_revision)
    engine.dispose()
    assert original_hash(database) == before
    assert migrations._read_revision(database) == old_revision
    assert migrations._validate_revision_schema(database, old_revision) == old_revision
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='session_context_projection'"
        ).fetchone() == (0,)

    # Exercise the old startup compatibility boundary: 0012 is complete and
    # exact, so an old loader accepts it without a guessed stamp or mutation.
    with monkeypatch.context() as old:
        old.setattr(migrations, "CURRENT_HEAD_REVISION", old_revision)
        old.setattr(migrations, "SUPPORTED_REVISIONS",
                    migrations.SUPPORTED_REVISIONS - {migrations.V110_CONTEXT_PROJECTION_REVISION})
        accepted = migrations.upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    assert accepted.current_revision == old_revision and accepted.backup_path is None
    assert original_hash(database) == before

    restored = migrations.upgrade_sqlite_database(f"sqlite+aiosqlite:///{database}")
    assert restored.previous_revision == old_revision
    assert restored.current_revision == migrations.V110_CONTEXT_PROJECTION_REVISION
    assert original_hash(database) == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM session_context_projection").fetchone() == (0,)
