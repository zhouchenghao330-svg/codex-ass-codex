"""Run the independent Rust reading comparison; no Codex or room state.

uv run python scripts/reading_lab.py
uv run python scripts/reading_lab.py --mode 1
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main():
    manifest = ROOT / "experiments/terminal-lab/Cargo.toml"
    suffix = ".exe" if os.name == "nt" else ""
    binary = manifest.parent / f"target/release/codex-room-reading-demo{suffix}"
    sources = [manifest, manifest.with_name("Cargo.lock"), manifest.parent / "src/reading_demo.rs",
               *(ROOT / "terminal-ui/src" / name for name in ("history.rs", "mermaid.rs", "table_copy.rs", "terminal.rs", "wide_backend.rs")),
               *(ROOT / "terminal-ui/vendor").rglob("*.rs")]
    if not binary.is_file() or any(path.stat().st_mtime > binary.stat().st_mtime for path in sources):
        cargo = shutil.which("cargo") or str(Path.home() / f".cargo/bin/cargo{suffix}")
        if not Path(cargo).is_file():
            raise SystemExit("这个独立开发 Demo 首次构建需要 Rust；正式 codexroom 启动不需要。")
        result = subprocess.run([cargo, "build", "--release", "--locked", "--manifest-path", str(manifest),
                                 "--bin", "codex-room-reading-demo"], cwd=ROOT)
        if result.returncode:
            raise SystemExit(result.returncode)
    environment = dict(os.environ)
    environment.pop("NO_COLOR", None)  # The comparison explicitly needs terminal colors.
    raise SystemExit(subprocess.call([str(binary), *sys.argv[1:]], env=environment))


if __name__ == "__main__":
    main()
