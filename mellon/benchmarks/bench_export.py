"""PDF export timing, split out because it is slow. Usage: bench_export.py REPO"""

import sys
import time
from pathlib import Path

from mellon import export, load_document

ROOT = Path(sys.argv[1]).resolve()
doc = load_document(ROOT / "SRS")
parents = {"SYS": load_document(ROOT / "SYS")}
start = time.perf_counter()
tex = export.render_latex(doc, parents=parents)
print(f"render_latex {time.perf_counter() - start:.2f}s, {len(tex):,d} bytes", flush=True)
start = time.perf_counter()
try:
    pdf = export.build_pdf(tex)
    print(f"build_pdf {time.perf_counter() - start:.1f}s, {len(pdf):,d} bytes", flush=True)
except Exception as e:
    print(f"build_pdf FAILED after {time.perf_counter() - start:.1f}s: {str(e)[-1500:]}", flush=True)
