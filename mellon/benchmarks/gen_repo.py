"""Build a synthetic repo with git fast-import: SYS (parent) and SRS (child), N objects each.

Every SRS non-heading object links to one SYS object, with correct acks, so all
links are OK. History: COMMITS commits after the first (default N), each touching
1-3 random objects. The result is packed, as a real repo is after gc.
Usage: gen_repo.py N DEST [COMMITS]
"""

import random
import subprocess
import sys
import time
from pathlib import Path

N = int(sys.argv[1])
ROOT = Path(sys.argv[2])
COMMITS = int(sys.argv[3]) if len(sys.argv) > 3 else N
rng = random.Random(42)

WORDS = (
    "The system shall monitor latch report verify the fault signal within limit under all "
    "operating conditions when commanded by the controller and maintain output state until reset "
    "is received over the bus with a response time below the specified threshold value"
).split()

ATTRS = """  <attributes>
    <attribute name="type" label="Type" kind="enum" required="true"><option>Heading</option><option>Requirement</option><option>Information</option></attribute>
    <attribute name="req-type" label="Requirement Type" kind="enum"><option>Functional</option><option>Performance</option><option>Safety</option></attribute>
    <attribute name="priority" label="Priority" kind="enum"><option>Must</option><option>Should</option><option>Could</option></attribute>
    <attribute name="status" label="Status" kind="enum" required="true"><option>Draft</option><option>Reviewed</option><option>Approved</option></attribute>
    <attribute name="verification-method" label="Verification Method" kind="enum"><option>Test</option><option>Analysis</option><option>Inspection</option></attribute>
  </attributes>
"""


def description(rev: int) -> str:
    # About 25 words; one object in six has a second paragraph.
    text = " ".join(rng.choice(WORDS) for _ in range(rng.randint(15, 35)))
    if rng.random() < 1 / 6:
        text += "\n" + " ".join(rng.choice(WORDS) for _ in range(rng.randint(10, 20)))
    return f"{text} (rev {rev})"


def is_heading(i: int) -> bool:
    return i % 25 == 1


def object_xml(doc: str, i: int, rev: int = 0) -> bytes:
    kind = "Heading" if is_heading(i) else "Requirement"
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<object id="{doc}-{i}">\n'
        f'  <attr name="type">{kind}</attr>\n'
        f'  <attr name="req-type">Functional</attr>\n'
        f'  <attr name="priority">Must</attr>\n'
        f'  <attr name="status">Draft</attr>\n'
        f'  <attr name="verification-method">Test</attr>\n'
        f"  <description>{description(rev)}</description>\n</object>\n"
    ).encode()


def manifest_xml(doc: str, parent: str | None) -> bytes:
    parents = f'  <parents>\n    <parent doc="{parent}"/>\n  </parents>\n' if parent else ""
    lines = []
    open_heading = False
    for i in range(1, N + 1):
        if is_heading(i):
            if open_heading:
                lines.append("    </ref>")
            lines.append(f'    <ref id="{doc}-{i}">')
            open_heading = True
        else:
            lines.append(f'      <ref id="{doc}-{i}"/>')
    if open_heading:
        lines.append("    </ref>")
    outline = "\n".join(lines)
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<document id="{doc}">\n'
        f"  <metadata>\n    <title>{doc} synthetic</title>\n  </metadata>\n{parents}{ATTRS}"
        f"  <outline>\n{outline}\n  </outline>\n</document>\n"
    ).encode()


def blob_cmd(path: str, data: bytes) -> bytes:
    return f"M 100644 inline {path}\ndata {len(data)}\n".encode() + data + b"\n"


def commit_cmd(mark: int, parent: int | None, message: str, files: list[bytes]) -> bytes:
    msg = message.encode()
    head = (
        f"commit refs/heads/master\nmark :{mark}\n"
        f"committer bench <bench@example.invalid> {1700000000 + mark} +0000\n"
        f"data {len(msg)}\n"
    ).encode() + msg + b"\n"
    if parent is not None:
        head += f"from :{parent}\n".encode()
    return head + b"".join(files) + b"\n"


start = time.perf_counter()
subprocess.run(["git", "init", "-q", "-b", "master", str(ROOT)], check=True)
for key, value in (("user.name", "bench"), ("user.email", "bench@example.invalid"), ("core.autocrlf", "false")):
    subprocess.run(["git", "-C", str(ROOT), "config", key, value], check=True)
marks = ROOT / ".git" / "bench-marks"


def fast_import(stream: bytes, *extra: str) -> None:
    subprocess.run(
        ["git", "-C", str(ROOT), "fast-import", "--quiet", f"--export-marks={marks}", *extra],
        input=stream, check=True,
    )


stream = bytearray()
files = [blob_cmd("SYS/SYS.xml", manifest_xml("SYS", None)), blob_cmd("SRS/SRS.xml", manifest_xml("SRS", "SYS"))]
for doc in ("SYS", "SRS"):
    files += [blob_cmd(f"{doc}/objects/{doc}-{i}.xml", object_xml(doc, i)) for i in range(1, N + 1)]
stream += commit_cmd(1, None, "create documents", files)

last_mark = {i: 1 for i in range(1, N + 1)}  # SYS targets only
for c in range(1, COMMITS + 1):
    mark = c + 1
    files = []
    for _ in range(rng.randint(1, 3)):
        doc = rng.choice(("SYS", "SRS"))
        i = rng.randint(1, N)
        files.append(blob_cmd(f"{doc}/objects/{doc}-{i}.xml", object_xml(doc, i, rev=c)))
        if doc == "SYS":
            last_mark[i] = mark
    stream += commit_cmd(mark, mark - 1, f"edit {c}", files)
fast_import(bytes(stream))

sha = {}
for line in marks.read_text().splitlines():
    m, s = line.split()
    sha[int(m[1:])] = s

links = []
for i in range(1, N + 1):
    if is_heading(i):
        continue
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<links source="SRS-{i}">\n'
        f'  <link target-doc="SYS" target-id="SYS-{i}" type="satisfies" ack="{sha[last_mark[i]][:7]}"/>\n</links>\n'
    ).encode()
    links.append(blob_cmd(f"SRS/links/SRS-{i}.xml", xml))
tail = commit_cmd(COMMITS + 2, None, "add links", links)
tail = tail.replace(b"data 9\nadd links\n", f"data 9\nadd links\nfrom {sha[COMMITS + 1]}\n".encode(), 1)
fast_import(tail, f"--import-marks={marks}", "--force")

subprocess.run(["git", "-C", str(ROOT), "reset", "-q", "--hard", "master"], check=True)
print(f"built N={N} commits={COMMITS + 2} links={len(links)} in {time.perf_counter() - start:.1f}s", flush=True)
