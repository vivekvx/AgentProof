"""Render, but never install, local launchd backup schedules."""

import argparse
import os
import plistlib
import shutil
import sys
import tempfile
from pathlib import Path


def executable_path(value, name):
    resolved = Path(value).resolve()
    if (
        not resolved.is_absolute()
        or not resolved.is_file()
        or not os.access(resolved, os.X_OK)
    ):
        raise ValueError(f"{name} must be an absolute executable file")
    return resolved


def plist(label, python, program, config, cadence, interval, pg_dump, openssl):
    return {
        "Label": label,
        "ProgramArguments": [
            python,
            program,
            "--config",
            str(config),
            "--cadence",
            cadence,
            "--pg-dump",
            str(pg_dump),
            "--openssl",
            str(openssl),
        ],
        "StartCalendarInterval": interval,
        "RunAtLoad": False,
        "ProcessType": "Background",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--program", type=Path, default=Path(__file__).with_name("local_backup.py")
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--pg-dump", type=Path, default=shutil.which("pg_dump"))
    parser.add_argument("--openssl", type=Path, default=shutil.which("openssl"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if (
        not args.config.is_absolute()
        or not args.program.is_absolute()
        or not args.python.is_absolute()
        or not args.output_dir.is_absolute()
    ):
        parser.error("use absolute paths; installation is intentionally separate")
    try:
        python = executable_path(args.python, "python")
        pg_dump = executable_path(args.pg_dump, "pg_dump")
        openssl = executable_path(args.openssl, "openssl")
    except (TypeError, ValueError) as error:
        parser.error(str(error))
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.output_dir.chmod(0o700)
    schedules = {
        "com.agentproof.backup.daily.plist": plist(
            "com.agentproof.backup.daily",
            str(python),
            str(args.program),
            args.config,
            "daily",
            {"Hour": 2, "Minute": 15},
            pg_dump,
            openssl,
        ),
        "com.agentproof.backup.monthly.plist": plist(
            "com.agentproof.backup.monthly",
            str(python),
            str(args.program),
            args.config,
            "monthly",
            {"Day": 1, "Hour": 3, "Minute": 15},
            pg_dump,
            openssl,
        ),
    }
    for name, value in schedules.items():
        path = args.output_dir / name
        with tempfile.NamedTemporaryFile(dir=args.output_dir, delete=False) as output:
            plistlib.dump(value, output, sort_keys=True)
            staged = Path(output.name)
        staged.chmod(0o600)
        os.replace(staged, path)
        print(path)


if __name__ == "__main__":
    main()
