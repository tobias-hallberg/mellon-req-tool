"""Generate repos and run the benchmarks, one at a time so they don't contend.

Usage (from mellon/): uv run --no-sync python benchmarks/run_all.py OUT_DIR [--export] [N ...]

Each size gets a fresh repo in OUT_DIR. Results land in OUT_DIR/<N>.<bench>.txt.
bench_core runs first and starts with no link index on disk, so it measures the
first-ever scan; bench_gui then opens a window with that index in place.
--export also times PDF export; it needs tectonic (see export.py for the lookup).
"""

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent


def run(out: Path, label: str, *cmd: str) -> None:
    with out.open("w") as f:
        code = subprocess.run([sys.executable, *cmd], stdout=f, stderr=subprocess.STDOUT).returncode
    print(f"{label}: exit {code}", flush=True)


def main() -> None:
    args = sys.argv[1:]
    export = "--export" in args
    args = [a for a in args if a != "--export"]
    out_dir = Path(args[0])
    sizes = [int(a) for a in args[1:]] or [500, 2000, 5000]
    out_dir.mkdir(parents=True, exist_ok=True)
    for n in sizes:
        repo = out_dir / f"r{n}"
        shutil.rmtree(repo, ignore_errors=True)
        subprocess.run([sys.executable, str(HERE / "gen_repo.py"), str(n), str(repo)], check=True)
        run(out_dir / f"{n}.core.txt", f"{n} core", str(HERE / "bench_core.py"), str(repo))
        run(
            out_dir / f"{n}.gui.txt", f"{n} gui",
            str(HERE / "bench_gui.py"), str(repo), str(out_dir / f"{n}.profile.txt"),
        )
        if export:
            run(out_dir / f"{n}.export.txt", f"{n} export", str(HERE / "bench_export.py"), str(repo))


if __name__ == "__main__":
    main()
