"""Portable source inventory. This does not attest historical execution."""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = (
    "go.mod",
    "go.sum",
    "package.json",
    "package-lock.json",
    "engine/pyproject.toml",
    "engine/uv.lock",
    "Dockerfile.dev",
    ".dockerignore",
    "Makefile",
)
TREES = {
    "engine/agentproof": {".py"},
    "apps/api": {".go"},
    "apps/web/src": {".ts", ".tsx", ".css"},
    "packages/contracts": {".json", ".go", ".md"},
    "db": {".sql", ".go"},
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inventory(root=ROOT):
    root = Path(root).resolve()
    paths = {root / p for p in FILES}
    for directory, suffixes in TREES.items():
        paths.update(
            p
            for p in (root / directory).rglob("*")
            if p.suffix in suffixes and "__pycache__" not in p.parts
        )
    files = {}
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("source symlink is not supported")
        files[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    return files


def manifest(root=ROOT):
    files = inventory(root)
    return {
        "schema_version": "source-inventory-v1",
        "files": files,
        "sha256": digest(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        ),
        "claim": "source inventory only; not execution attestation or Git commit",
    }


def validate(value, root=ROOT):
    if value != manifest(root):
        raise ValueError("unsupported or altered source inventory")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inventory", "validate"])
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    if args.action == "inventory":
        with args.file.open("x") as target:
            json.dump(manifest(), target, indent=2)
    else:
        validate(json.loads(args.file.read_text()))
        print("Source inventory matches; execution provenance is not established.")


if __name__ == "__main__":
    main()
