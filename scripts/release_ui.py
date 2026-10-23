"""Validate and stage the six public UI assets; used by GitHub Actions."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from codex_room.ui_release import TARGETS, UI_VERSION, asset_name, sha256


def verify_version(tag):
    if tag != f"v{UI_VERSION}":
        raise SystemExit(f"Tag {tag!r} must match pinned UI version v{UI_VERSION}")
    for file, key in (("pyproject.toml", "project"), ("terminal-ui/Cargo.toml", "package")):
        if tomllib.loads((ROOT / file).read_text())[key]["version"] != UI_VERSION:
            raise SystemExit(f"Version mismatch: {file}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify-version", "stage", "checksums"))
    parser.add_argument("--tag", default=f"v{UI_VERSION}")
    parser.add_argument("--target", choices=sorted(TARGETS.values()))
    parser.add_argument("--directory", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    verify_version(args.tag)
    if args.action == "verify-version":
        return
    args.directory.mkdir(parents=True, exist_ok=True)
    if args.action == "stage":
        if not args.target:
            parser.error("stage requires --target")
        suffix = ".exe" if "windows" in args.target else ""
        source = ROOT / "terminal-ui/target" / args.target / "release" / f"codex-room-ui{suffix}"
        subprocess.run([str(source), "--help"], check=True)
        shutil.copy2(source, args.directory / asset_name(args.target))
    else:
        expected = {asset_name(target) for target in TARGETS.values()}
        actual = {p.name for p in args.directory.iterdir() if p.name != "SHA256SUMS"}
        if actual != expected:
            raise SystemExit(f"Release assets mismatch. Missing: {expected-actual}; unexpected: {actual-expected}")
        text = "".join(f"{sha256(args.directory / name)}  {name}\n" for name in sorted(expected))
        (args.directory / "SHA256SUMS").write_text(text, encoding="utf-8", newline="\n")
        print(text, end="")


if __name__ == "__main__":
    main()
