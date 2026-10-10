#!/usr/bin/env python3
"""Copy the bundled runtime to a new workspace without secrets or side effects."""
import argparse
import json
from pathlib import Path
import shutil
import sys

SKILL_ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = ("baby_video", "scripts", "remotion", "examples", "tests")
ROOT_FILES = ("README.md", "package.json", "package-lock.json", "tsconfig.json", ".env.example", ".gitignore")
SUFFIXES = {".py", ".mjs", ".ts", ".tsx", ".json", ".txt"}


def initialize(destination: Path) -> Path:
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Workspace already exists; resume it instead of overwriting")
    runtime = SKILL_ROOT / "runtime"
    files = [(runtime / name, Path(name)) for name in ROOT_FILES]
    for directory in DIRECTORIES:
        for source in sorted((runtime / directory).iterdir()):
            if source.suffix in SUFFIXES and source.is_file() and not source.is_symlink():
                files.append((source, source.relative_to(runtime)))
    files.append((SKILL_ROOT / "references/instagram-caption.md", Path("references/instagram-caption.md")))
    files.append((SKILL_ROOT / "LICENSE", Path("LICENSE")))
    if any(not source.is_file() or source.is_symlink() for source, _ in files):
        raise FileNotFoundError("Bundled runtime is incomplete")
    destination.mkdir(parents=True, exist_ok=False)
    for source, relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return destination


def main():
    parser = argparse.ArgumentParser(description="Initialize a new baby-video workspace; no network or API calls")
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    try:
        destination = initialize(args.workspace)
    except (OSError, ValueError) as error:
        print(json.dumps({"error": str(error), "network_called": False}), file=sys.stderr)
        return 2
    print(json.dumps({"workspace": str(destination), "network_called": False,
                      "credentials_copied": False, "dependencies_installed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
