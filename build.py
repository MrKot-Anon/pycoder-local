#!/usr/bin/env python3
"""Build a reproducible zipapp, including pinned, pre-downloaded UI dependencies."""

import argparse
import json
import os
from pathlib import Path
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
BOOTSTRAP = '''import sys
if sys.version_info < (3, 11):
    sys.exit("PyCoder requires Python 3.11 or newer.")
from pycoder.cli import main
raise SystemExit(main())
'''


def build(output: Path) -> Path:
    output = output.absolute()
    source_files = sorted((ROOT / "pycoder").rglob("*.py"))
    source_files += sorted(p for p in (ROOT / "pycoder/instructions").iterdir() if p.suffix in {".md", ".lark", ".sbpl"})
    extras = [ROOT / name for name in ("build.py", "run.py", "README.md", "pyproject.toml", "requirements-ui.lock", "requirements-e2ee.txt", ".gitignore", "LICENSE", "NOTICE")]
    extras += sorted((ROOT / "tests").rglob("*.py"))
    extras += sorted(p for p in (ROOT / "tests/fixtures").rglob("*")
                     if p.is_file() and p.name != ".DS_Store" and "__pycache__" not in p.parts)
    extras += sorted((ROOT / "scripts").rglob("*.py"))
    protected = {p.resolve() for p in source_files + extras}
    protected.update((ROOT / name).resolve() for name in ("build.py", "run.py", "LICENSE", "NOTICE"))
    if output.resolve() in protected or any(output.resolve().is_relative_to((ROOT / name).resolve())
                                           for name in ("pycoder", "vendor", "tests", "scripts")):
        raise ValueError("Build output must not overwrite the source package")
    entries = {"__main__.py": BOOTSTRAP.encode("utf-8")}
    entries.update({str(p.relative_to(ROOT).as_posix()): p.read_bytes() for p in source_files})
    project = {p.relative_to(ROOT).as_posix(): p.relative_to(ROOT).as_posix() for p in source_files}
    for path in extras:
        name = path.relative_to(ROOT).as_posix()
        entries["_source/" + name] = path.read_bytes()
        project[name] = "_source/" + name
    vendor = ROOT / "vendor"
    if not (vendor / "textual/__init__.py").is_file():
        raise ValueError("Missing UI dependencies. Run: python3 -m pip install --target vendor --only-binary=:all: -r requirements-ui.lock")
    for path in sorted(vendor.rglob("*")):
        relative = path.relative_to(vendor)
        if not path.is_file() or any(p in {"__pycache__", "bin", "tests"} for p in relative.parts) or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.suffix in {".so", ".dylib", ".pyd"}:
            raise ValueError(f"Native dependency cannot be bundled as pure zipapp: {relative}")
        if str(relative) in entries:
            raise ValueError(f"Dependency conflicts with application: {relative}")
        entries[relative.as_posix()] = path.read_bytes()
        project["vendor/" + relative.as_posix()] = relative.as_posix()
    for name in ("LICENSE", "NOTICE"):
        entries[name] = (ROOT / name).read_bytes()
    entries["_source/manifest.json"] = json.dumps(project, sort_keys=True, ensure_ascii=False).encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pycoder-build-", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"#!/usr/bin/env python3\n")
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, contents in sorted(entries.items()):
                    info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o755)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", "-o", type=Path, default=ROOT / "dist/pycoder",
                        help="Output file (default: dist/pycoder beside this script)")
    args = parser.parse_args(argv)
    try:
        artifact = build(args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Build failed: {error}\n")
    print(f"Собран: {artifact} ({artifact.stat().st_size} байт)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
