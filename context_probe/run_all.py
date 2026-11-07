"""Run all three experiments in order and save their outputs.

Usage (from this directory):
    python run_all.py            # uses this script's interpreter
    python run_all.py --fresh    # recreates .venv first (Windows: py -3.12)

Experiments:
  1. probe.py        - record gap: Message-only vs Task replies, store dumps
  2. chain_probe.py  - record laundering A -> B -> C
  3. fix_probe.py    - client-side reply-type contract, 6-case test matrix
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent

EXPERIMENTS = [
    ("probe.py", "probe_output.txt"),
    ("chain_probe.py", "chain_output.txt"),
    ("fix_probe.py", "fix_output.txt"),
]


def fresh_venv() -> None:
    venv = HERE / ".venv"
    if venv.exists():
        import shutil

        shutil.rmtree(venv)
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    pip = venv / ("Scripts/pip.exe" if sys.platform == "win32" else "bin/pip")
    subprocess.run([str(pip), "install", "-r", str(HERE / "requirements.txt")], check=True)


def venv_python() -> str:
    """Prefer the local .venv interpreter; fall back to the current one."""
    cand = HERE / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    return str(cand) if cand.exists() else sys.executable


def main() -> None:
    if "--fresh" in sys.argv:
        print("Recreating .venv with pinned dependencies ...")
        fresh_venv()

    failed = []
    for script, outfile in EXPERIMENTS:
        print(f"\n{'#' * 70}\n# RUNNING {script}\n{'#' * 70}", flush=True)
        with open(HERE / outfile, "w", encoding="utf-8") as fh:
            proc = subprocess.run(
                [venv_python(), str(HERE / script)],
                stdout=fh,
                stderr=subprocess.STDOUT,
                cwd=HERE,
            )
        if proc.returncode != 0:
            failed.append(script)
            print(f"!! {script} FAILED (exit {proc.returncode}) - see {outfile}")
        else:
            print(f"ok - output saved to {outfile}")

    print("\nDone.", "All experiments passed." if not failed else f"Failures: {failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
