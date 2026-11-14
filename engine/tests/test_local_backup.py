"""Local-only encrypted scheduled backup controls."""

import importlib.util
import json
import plistlib
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "scripts/local_backup.py"
SPEC = importlib.util.spec_from_file_location("local_backup", SOURCE)
assert SPEC and SPEC.loader
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)
FRESHNESS_SOURCE = SOURCE.with_name("verify_backup_freshness.py")
FRESHNESS_SPEC = importlib.util.spec_from_file_location(
    "verify_backup_freshness", FRESHNESS_SOURCE
)
assert FRESHNESS_SPEC and FRESHNESS_SPEC.loader
freshness = importlib.util.module_from_spec(FRESHNESS_SPEC)
FRESHNESS_SPEC.loader.exec_module(freshness)


def certificate(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    key, cert = tmp_path / "recipient.key", tmp_path / "recipient.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=agentproof-test",
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    cert.chmod(0o600)
    return key, cert


def configuration(tmp_path, cert):
    directory = tmp_path / "backups"
    value = {
        "schema_version": backup.SCHEMA,
        "source_database_url": "postgresql://synthetic",
        "backup_directory": str(directory),
        "recipient_certificate": str(cert),
        "key_recovery": {"method": "manual", "instructions": "owner-held test key"},
        "off_host_destination": "documented-but-not-enabled://owner-provided",
    }
    path = tmp_path / "backup-config.json"
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return value, directory, path


def fake_dump(monkeypatch):
    original = backup.run

    def run(command):
        if command[0] == "pg_dump":
            Path(command[command.index("--file") + 1]).write_bytes(b"PGDMPsynthetic")
            return
        return original(command)

    monkeypatch.setattr(backup, "run", run)


def test_encrypted_daily_backup_is_idempotent_and_private(tmp_path, monkeypatch):
    key, cert = certificate(tmp_path)
    config, directory, _ = configuration(tmp_path, cert)
    fake_dump(monkeypatch)
    now = datetime(2026, 11, 13, 2, 15, tzinfo=UTC)
    first = backup.create(config, directory, cert, "daily", now, "pg_dump", "openssl")
    second = backup.create(config, directory, cert, "daily", now, "pg_dump", "openssl")
    assert first["status"] == "created" and second["status"] == "reused"
    archive, manifest = Path(first["archive"]), Path(first["manifest"])
    assert archive.read_bytes() != b"PGDMPsynthetic"
    assert archive.stat().st_mode & 0o077 == 0
    metadata = json.loads(manifest.read_text())
    assert metadata["ciphertext_sha256"] == backup.sha256(archive)
    restored = tmp_path / "restored.pgdump"
    subprocess.run(
        [
            "openssl",
            "cms",
            "-decrypt",
            "-binary",
            "-inform",
            "DER",
            "-in",
            str(archive),
            "-recip",
            str(cert),
            "-inkey",
            str(key),
            "-out",
            str(restored),
        ],
        check=True,
        capture_output=True,
    )
    assert restored.read_bytes() == b"PGDMPsynthetic"
    assert metadata["off_host_transfer"] == "not attempted"

    _wrong_key, wrong_cert = certificate(tmp_path / "wrong-recipient")
    wrong = subprocess.run(
        [
            "openssl",
            "cms",
            "-decrypt",
            "-binary",
            "-inform",
            "DER",
            "-in",
            str(archive),
            "-recip",
            str(wrong_cert),
            "-inkey",
            str(_wrong_key),
            "-out",
            str(tmp_path / "wrong.pgdump"),
        ],
        capture_output=True,
        check=False,
    )
    assert wrong.returncode != 0
    corrupted = tmp_path / "corrupted.cms"
    raw = bytearray(archive.read_bytes())
    raw[-1] ^= 1
    corrupted.write_bytes(raw)
    corrupted_decrypt = subprocess.run(
        [
            "openssl",
            "cms",
            "-decrypt",
            "-binary",
            "-inform",
            "DER",
            "-in",
            str(corrupted),
            "-recip",
            str(cert),
            "-inkey",
            str(key),
            "-out",
            str(tmp_path / "corrupted.pgdump"),
        ],
        capture_output=True,
        check=False,
    )
    assert corrupted_decrypt.returncode != 0


def test_invalid_key_and_interrupted_backup_leave_no_scheduled_artifact(
    tmp_path, monkeypatch
):
    _, cert = certificate(tmp_path)
    config, directory, _ = configuration(tmp_path, cert)
    cert.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        backup.create(
            config,
            directory,
            cert,
            "monthly",
            datetime(2026, 11, 13, tzinfo=UTC),
            "pg_dump",
            "openssl",
        )
    cert.chmod(0o600)
    monkeypatch.setattr(
        backup,
        "run",
        lambda _command: (_ for _ in ()).throw(RuntimeError("interrupted")),
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        backup.create(
            config,
            directory,
            cert,
            "monthly",
            datetime(2026, 11, 13, tzinfo=UTC),
            "pg_dump",
            "openssl",
        )
    assert not list(directory.glob("*.cms"))
    monkeypatch.undo()
    fake_dump(monkeypatch)
    result = backup.create(
        config,
        directory,
        cert,
        "monthly",
        datetime(2026, 11, 13, tzinfo=UTC),
        "pg_dump",
        "openssl",
    )
    assert result["status"] == "created"


def test_scheduler_rejects_nonprivate_config_and_keeps_manual_recovery(tmp_path):
    _, cert = certificate(tmp_path)
    config, _, path = configuration(tmp_path, cert)
    path.chmod(0o644)
    assert not backup.private_path(path)
    config["key_recovery"] = {"method": "automatic", "instructions": "no"}
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    with pytest.raises(ValueError, match="manual"):
        backup.load_config(path)


def test_schedule_renderer_prepares_daily_and_monthly_without_installing(tmp_path):
    config = (tmp_path / "backup-config.json").resolve()
    config.write_text("{}")
    output = (tmp_path / "prepared").resolve()
    script = SOURCE.parents[0] / "render_backup_schedule.py"
    command = [
        "python3",
        str(script),
        "--config",
        str(config),
        "--program",
        str(SOURCE.resolve()),
        "--pg-dump",
        "/usr/bin/true",
        "--openssl",
        "/usr/bin/true",
        "--output-dir",
        str(output),
    ]
    subprocess.run(command, check=True, capture_output=True)
    first = {
        path.name: path.read_bytes()
        for path in output.glob("com.agentproof.backup.*.plist")
    }
    subprocess.run(command, check=True, capture_output=True)
    assert first == {
        path.name: path.read_bytes()
        for path in output.glob("com.agentproof.backup.*.plist")
    }
    daily = plistlib.loads((output / "com.agentproof.backup.daily.plist").read_bytes())
    monthly = plistlib.loads(
        (output / "com.agentproof.backup.monthly.plist").read_bytes()
    )
    assert Path(daily["ProgramArguments"][0]).is_absolute()
    assert "python" in Path(daily["ProgramArguments"][0]).name
    assert (
        daily["ProgramArguments"][daily["ProgramArguments"].index("--cadence") + 1]
        == "daily"
    )
    assert "/usr/bin/true" in daily["ProgramArguments"]
    assert (output / "com.agentproof.backup.daily.plist").stat().st_mode & 0o077 == 0
    assert monthly["StartCalendarInterval"] == {"Day": 1, "Hour": 3, "Minute": 15}


def test_rendered_daily_schedule_invokes_idempotent_synthetic_backup(tmp_path):
    _key, cert = certificate(tmp_path)
    _config, directory, config_path = configuration(tmp_path, cert)
    fake_dump = tmp_path / "pg_dump"
    fake_dump.write_text(
        "#!/bin/sh\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--file" ]; then shift; output=$1; fi\n'
        "  shift\n"
        "done\n"
        'printf PGDMPscheduled > "$output"\n'
    )
    fake_dump.chmod(0o700)
    prepared = (tmp_path / "prepared").resolve()
    subprocess.run(
        [
            sys.executable,
            str(SOURCE.with_name("render_backup_schedule.py")),
            "--config",
            str(config_path),
            "--program",
            str(SOURCE),
            "--python",
            sys.executable,
            "--pg-dump",
            str(fake_dump),
            "--openssl",
            shutil.which("openssl") or "/usr/bin/openssl",
            "--output-dir",
            str(prepared),
        ],
        check=True,
        capture_output=True,
    )
    daily = plistlib.loads(
        (prepared / "com.agentproof.backup.daily.plist").read_bytes()
    )
    command = daily["ProgramArguments"] + ["--at", "2026-11-13T02:16:00+00:00"]
    first = subprocess.run(command, check=True, capture_output=True, text=True)
    second = subprocess.run(command, check=True, capture_output=True, text=True)
    assert json.loads(first.stdout)["status"] == "created"
    assert json.loads(second.stdout)["status"] == "reused"
    assert len(list(directory.glob("*.pgdump.cms"))) == 1


def test_freshness_detects_missed_calendar_run_after_shutdown(tmp_path, monkeypatch):
    _key, cert = certificate(tmp_path)
    config, directory, path = configuration(tmp_path, cert)
    fake_dump(monkeypatch)
    after_daily_due = datetime(2026, 11, 13, 4, 0, tzinfo=UTC)
    stale = freshness.assess(path, "daily", after_daily_due, timedelta())
    assert stale["status"] == "stale" and stale["overdue"] is True
    not_due = freshness.assess(
        path,
        "daily",
        datetime(2026, 11, 14, 2, 14, tzinfo=UTC),
        timedelta(),
    )
    assert not_due["status"] == "not_due"
    backup.create(
        config,
        directory,
        cert,
        "daily",
        datetime(2026, 11, 13, 2, 16, tzinfo=UTC),
        "pg_dump",
        "openssl",
    )
    fresh = freshness.assess(path, "daily", after_daily_due, timedelta())
    assert fresh["status"] == "fresh" and fresh["overdue"] is True
