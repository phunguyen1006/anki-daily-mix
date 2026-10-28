# Copyright: Anki Daily Mix contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Build an installable ``.ankiaddon`` archive.

Anki requires the add-on folder to be a valid Python package name.  This
repository is called ``anki-daily-mix`` (a perfectly good repository name, but
not importable), so installing the checkout directly is fragile.  The archive
produced here contains the same files under the clean package name from
``manifest.json``.

Usage::

    python tools/build_addon.py
    python tools/build_addon.py --output dist/anki_daily_mix.ankiaddon

The build is deterministic: entries are sorted, timestamps are fixed, and no
``__pycache__`` or test files are included.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "dist" / "anki_daily_mix.ankiaddon"

#: Root-level files that must ship with the add-on.
ROOT_FILES = (
    "__init__.py",
    "manifest.json",
    "config.json",
    "config.md",
    "LICENSE",
)

#: Everything below these directories is included (minus bytecode caches).
SOURCE_DIRS = (
    "src",
    "web",
)

_IGNORED_SUFFIXES = {".pyc", ".pyo"}
_IGNORED_DIRS = {"__pycache__"}


def _collect_files() -> list[Path]:
    files: list[Path] = []
    for name in ROOT_FILES:
        path = REPO_ROOT / name
        if not path.is_file():
            raise SystemExit("Missing required file: %s" % path)
        files.append(path)

    for directory in SOURCE_DIRS:
        root = REPO_ROOT / directory
        if not root.is_dir():
            raise SystemExit("Missing required directory: %s" % root)
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in _IGNORED_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in _IGNORED_SUFFIXES:
                continue
            files.append(path)

    return sorted(files, key=lambda item: item.relative_to(REPO_ROOT).as_posix())


def _validate(files: list[Path]) -> str:
    manifest_path = REPO_ROOT / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit("manifest.json is not valid JSON: %s" % exc) from exc

    package = str(manifest.get("package") or "").strip()
    if not package:
        raise SystemExit("manifest.json must contain a non-empty 'package' field")
    if not package.isidentifier():
        raise SystemExit(
            "manifest.json 'package' must be a valid Python identifier, got %r"
            % package
        )

    try:
        json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit("config.json is not valid JSON: %s" % exc) from exc

    names = {path.relative_to(REPO_ROOT).as_posix() for path in files}
    for required in ("__init__.py", "manifest.json", "config.json", "web/index.html"):
        if required not in names:
            raise SystemExit("The archive would be missing %s" % required)

    return package


def build(output: Path) -> Path:
    files = _collect_files()
    package = _validate(files)

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    # A fixed timestamp keeps repeated builds byte-for-byte identical.
    timestamp = (2024, 1, 1, 0, 0, 0)

    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for path in files:
            relative = path.relative_to(REPO_ROOT).as_posix()
            info = zipfile.ZipInfo(relative, date_time=timestamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())

    print("Built %s" % output)
    print("Package: %s" % package)
    print("Files: %d" % len(files))
    print("Size: %.1f KiB" % (output.stat().st_size / 1024))
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Destination .ankiaddon file (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        build(args.output)
    except OSError as exc:
        print("Build failed: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
