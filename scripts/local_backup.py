"""Create one encrypted local PostgreSQL archive for a scheduled cadence.

This program never uploads, deletes, or prunes an archive. The scheduler invokes
it for ``daily`` or ``monthly``; it reuses an already verified artifact for the
same UTC period so retries are idempotent.
"""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = "agentproof-local-backup-v1"
CADENCES = {"daily", "monthly"}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def private_path(path):
    return path.is_absolute() and path.exists() and path.stat().st_mode & 0o077 == 0


def load_config(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("backup configuration is unreadable") from error
    required = {
        "schema_version",
        "source_database_url",
        "backup_directory",
        "recipient_certificate",
        "key_recovery",
    }
    if set(value) - {
        "schema_version",
        "source_database_url",
        "backup_directory",
        "recipient_certificate",
        "key_recovery",
        "off_host_destination",
    } or not required <= set(value):
        raise ValueError("backup configuration fields differ")
    if value["schema_version"] != SCHEMA or not isinstance(
        value["source_database_url"], str
    ):
        raise ValueError("backup configuration identity differs")
    directory = Path(value["backup_directory"])
    certificate = Path(value["recipient_certificate"])
    recovery = value["key_recovery"]
    if (
        not directory.is_absolute()
        or not certificate.is_absolute()
        or not certificate.is_file()
    ):
        raise ValueError("backup paths must be absolute and certificate must exist")
    if (
        not isinstance(recovery, dict)
        or set(recovery) != {"method", "instructions"}
        or recovery["method"] != "manual"
        or not isinstance(recovery["instructions"], str)
        or not recovery["instructions"].strip()
    ):
        raise ValueError("manual key recovery instructions are required")
    destination = value.get("off_host_destination")
    if destination is not None and (
        not isinstance(destination, str) or not destination.strip()
    ):
        raise ValueError("off-host destination must be a documented string or null")
    return value, directory, certificate


def period(now, cadence):
    return now.strftime("%Y-%m-%d" if cadence == "daily" else "%Y-%m")


def verify(archive, manifest):
    try:
        metadata = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        archive.is_file()
        and metadata.get("schema_version") == SCHEMA
        and metadata.get("ciphertext_sha256") == sha256(archive)
        and metadata.get("ciphertext_bytes") == archive.stat().st_size
    )


def run(command):
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr[-500:] or "backup command failed")


def create(config, directory, certificate, cadence, now, pg_dump, openssl):
    if cadence not in CADENCES:
        raise ValueError("invalid backup cadence")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    if not private_path(directory) or not private_path(certificate):
        raise ValueError("backup directory and recipient certificate must be private")
    stamp = period(now, cadence)
    archive = directory / f"agentproof-{cadence}-{stamp}.pgdump.cms"
    manifest = directory / f"agentproof-{cadence}-{stamp}.json"
    if archive.exists() or manifest.exists():
        if verify(archive, manifest):
            return {
                "status": "reused",
                "archive": str(archive),
                "manifest": str(manifest),
            }
        raise ValueError("existing scheduled artifact is incomplete or invalid")
    with tempfile.TemporaryDirectory(
        prefix="agentproof-backup-", dir=directory
    ) as raw_dir:
        raw = Path(raw_dir) / "archive.pgdump"
        encrypted = Path(raw_dir) / "archive.cms"
        run(
            [
                pg_dump,
                "--format=custom",
                "--file",
                str(raw),
                config["source_database_url"],
            ]
        )
        if not raw.read_bytes().startswith(b"PGDMP"):
            raise ValueError("pg_dump did not create a PostgreSQL archive")
        plain_hash = sha256(raw)
        run(
            [
                openssl,
                "cms",
                "-encrypt",
                "-binary",
                "-aes-256-gcm",
                "-in",
                str(raw),
                "-out",
                str(encrypted),
                "-outform",
                "DER",
                str(certificate),
            ]
        )
        os.chmod(encrypted, 0o600)
        metadata = {
            "schema_version": SCHEMA,
            "cadence": cadence,
            "period": stamp,
            "created_at": now.isoformat(),
            "archive": archive.name,
            "ciphertext_sha256": sha256(encrypted),
            "ciphertext_bytes": encrypted.stat().st_size,
            "plaintext_sha256": plain_hash,
            "encryption": "openssl-cms-aes-256-gcm",
            "off_host_destination": config.get("off_host_destination"),
            "off_host_transfer": "not attempted",
        }
        staged_manifest = Path(raw_dir) / "manifest.json"
        staged_manifest.write_text(json.dumps(metadata, sort_keys=True) + "\n")
        os.chmod(staged_manifest, 0o600)
        os.replace(encrypted, archive)
        os.replace(staged_manifest, manifest)
    return {"status": "created", "archive": str(archive), "manifest": str(manifest)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cadence", choices=sorted(CADENCES), required=True)
    parser.add_argument("--at", help="UTC instant for a deterministic scheduler test")
    parser.add_argument("--pg-dump", default=os.getenv("AGENTPROOF_PG_DUMP", "pg_dump"))
    parser.add_argument("--openssl", default=os.getenv("AGENTPROOF_OPENSSL", "openssl"))
    args = parser.parse_args()
    now = datetime.fromisoformat(args.at) if args.at else datetime.now(UTC)
    if now.tzinfo is None:
        parser.error("--at must include a timezone")
    if not private_path(args.config):
        parser.error("backup configuration must be an existing private absolute file")
    config, directory, certificate = load_config(args.config)
    try:
        result = create(
            config,
            directory,
            certificate,
            args.cadence,
            now.astimezone(UTC),
            args.pg_dump,
            args.openssl,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}))
        raise SystemExit(1) from error
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
