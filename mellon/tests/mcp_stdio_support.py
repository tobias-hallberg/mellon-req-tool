"""Shared harness for the stdio tests of the MCP server.

Starts the installed ``mellon-mcp`` program as an agent's client does, talks to it
with the ``mcp`` client library or over raw pipes, and builds the throwaway
repositories the tests serve: copies of the ``req-test`` fixture, or small
repositories under pytest's temporary folders. Test modules import the module
fixtures ``ident_home``, ``neutral_dir``, ``plain_template`` and ``rich_template``
from here.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import anyio
import anyio.from_thread
import mcp.types
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.conftest import _clean_worktree, _ensure_req_test_fixture

WINDOWS = sys.platform == "win32"
MELLON_MCP = shutil.which("mellon-mcp", path=str(Path(sys.executable).parent))
PROJECT_ROOT = Path(__file__).resolve().parents[2]
REQ_TEST = PROJECT_ROOT / "req-test"
FIXTURE_DOCS = {"VCU3-SRS", "VCU3-STS", "VCU3-SyRS"}
CALL_TIMEOUT = 120


requires_mellon_mcp = pytest.mark.skipif(
    MELLON_MCP is None, reason="the mellon-mcp program isn't installed in this environment"
)


SVG = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20" viewBox="0 0 40 20">'
    '<rect x="1" y="1" width="38" height="18" fill="none" stroke="black"/></svg>\n'
)


TABLE_XML = """  <table>
    <columns>
      <col align="left"/>
      <col align="right"/>
    </columns>
    <row header="true">
      <cell>Signal</cell>
      <cell>Limit</cell>
    </row>
    <row>
      <cell>Coolant</cell>
      <cell>106</cell>
    </row>
  </table>
"""


TABLE = {
    "columns": [{"align": "left"}, {"align": "right"}],
    "rows": [
        {"header": True, "cells": ["Signal", "Limit"]},
        {"header": False, "cells": ["Coolant", "106"]},
    ],
}


MINI_MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<document id="{doc_id}">
  <metadata>
    <title>{title}</title>
    <author>Blind Test</author>
    <date>2026-11-11</date>
  </metadata>
  <parents/>
  <attributes>
    <attribute name="type" label="Type" kind="enum" required="true">
      <option>Heading</option>
      <option>Requirement</option>
    </attribute>
  </attributes>
  <outline>
    <ref id="{doc_id}-1"/>
  </outline>
</document>
"""


MINI_OBJECT = """<?xml version="1.0" encoding="UTF-8"?>
<object id="{obj_id}">
  <attr name="type">Requirement</attr>
  <description>{text}</description>
</object>
"""


# The stand-in typesetter and draw.io: each prints to standard output and
# standard error, logs its arguments, and writes the PDF it is asked for.
STANDIN_SCRIPT = r'''
import pathlib
import sys


def pdf_bytes():
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] >>"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def option(args, *names):
    for i, arg in enumerate(args):
        if arg in names and i + 1 < len(args):
            return args[i + 1]
        for name in names:
            if name.startswith("--") and arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return None


def write_pdf(path):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf_bytes())


mode, log_dir, *args = sys.argv[1:]
print("BLIND-STANDIN " + mode + " says hello on standard output", flush=True)
print('{"jsonrpc": "2.0", "BLIND-STANDIN": "' + mode + '"', flush=True)
sys.stderr.write("BLIND-STANDIN " + mode + " says hello on standard error\n")
with open(pathlib.Path(log_dir) / (mode + ".log"), "a", encoding="utf-8") as log:
    log.write(repr(args) + "\n")
if "--version" in args or "-V" in args:
    print(mode + " 99.0.0", flush=True)
    sys.exit(0)
if mode == "tectonic":
    outdir = option(args, "-o", "--outdir")
    for arg in args:
        if arg.endswith(".tex"):
            tex = pathlib.Path(arg)
            target = pathlib.Path(outdir) if outdir else tex.parent
            write_pdf(target / (tex.stem + ".pdf"))
else:
    output = option(args, "-o", "--output")
    sources = [pathlib.Path(a) for a in args
               if not a.startswith("-") and a != output and pathlib.Path(a).exists()]
    for source in sources:
        for svg in (sorted(source.glob("*.svg")) if source.is_dir() else [source]):
            if output and output.lower().endswith(".pdf") and not source.is_dir():
                write_pdf(output)
            else:
                write_pdf(pathlib.Path(output or svg.parent) / (svg.stem + ".pdf"))
sys.exit(0)
'''


# ---------------------------------------------------------------------------
# Environment, git and file helpers
# ---------------------------------------------------------------------------


def child_env(home: Path, **extra: str | None) -> dict[str, str]:
    """This process's environment for a child, with ``home`` as its home.

    The git identity, if any, comes from ``home``'s ``.gitconfig``. Variables
    that choose the folder to serve or a program are left out unless given.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_")
        and k not in ("CLAUDE_PROJECT_DIR", "MELLON_TECTONIC", "MELLON_DRAWIO")
    }
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        GIT_CONFIG_NOSYSTEM="1",
    )
    if WINDOWS:
        drive, rest = os.path.splitdrive(str(home))
        env.update(HOMEDRIVE=drive, HOMEPATH=rest)
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


def git(root: Path, *args: str, env: dict[str, str], check: bool = True) -> str:
    r = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in setup: {r.stderr.strip()}")
    return r.stdout


def write_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def clone_of(template: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, dest)
    return dest


def make_doc_repo(root: Path, doc_id: str, env: dict[str, str]) -> Path:
    """A repository holding one document ``doc_id`` with one object."""
    write_lf(root / doc_id / f"{doc_id}.xml", MINI_MANIFEST.format(doc_id=doc_id, title=f"{doc_id} document"))
    write_lf(
        root / doc_id / "objects" / f"{doc_id}-1.xml",
        MINI_OBJECT.format(obj_id=f"{doc_id}-1", text=f"The {doc_id} system shall exist."),
    )
    git(root, "init", "-q", env=env)
    git(root, "add", "-A", env=env)
    git(root, "commit", "-q", "-m", f"Add {doc_id}", env=env)
    return root


def make_empty_repo(root: Path, env: dict[str, str], *, commit: bool) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q", env=env)
    if commit:
        write_lf(root / "README.md", "Nothing but a readme.\n")
        git(root, "add", "-A", env=env)
        git(root, "commit", "-q", "-m", "Add a readme", env=env)
    return root


def tree_hashes(root: Path, *, include_git: bool, skip: tuple[str, ...] = ()) -> dict[str, str]:
    """SHA-256 of each file under ``root``, by posix path relative to it."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        if here == root and not include_git and ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            p = here / name
            rel = p.relative_to(root).as_posix()
            if (not include_git and rel == ".git") or any(rel == s or rel.startswith(s + "/") for s in skip):
                continue
            try:
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError as exc:
                out[rel] = f"<unreadable: {exc.__class__.__name__}>"
    return out


def changed(before: dict[str, str], after: dict[str, str]) -> set[str]:
    return {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}


def fold(p: str) -> str:
    return p.casefold() if WINDOWS else p


def rel_paths(root: Path, paths) -> set[str]:
    """``paths`` (absolute or relative to ``root``) as folded posix paths relative to ``root``."""
    out = set()
    for p in paths:
        pp = Path(p)
        if not pp.is_absolute():
            pp = root / pp
        try:
            rel = os.path.relpath(pp, root)
        except ValueError:  # another drive
            rel = str(pp)
        out.add(fold(rel.replace(os.sep, "/")))
    return out


def ref_state(root: Path, env: dict[str, str]) -> str:
    """HEAD, the branch it names and every ref, as one string."""
    return (
        git(root, "for-each-ref", "--format=%(refname) %(objectname)", env=env)
        + "HEAD "
        + git(root, "rev-parse", "HEAD", env=env)
        + git(root, "symbolic-ref", "-q", "HEAD", env=env, check=False)
    )


def norm_text(text: str) -> str:
    return os.path.normcase(text) if WINDOWS else text


def standin_programs(folder: Path, log_dir: Path) -> dict[str, str]:
    """Write the stand-in typesetter and draw.io; return their env variables."""
    folder.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    script = folder / "standin.py"
    write_lf(script, STANDIN_SCRIPT)
    paths = {}
    for mode in ("tectonic", "drawio"):
        if WINDOWS:
            prog = folder / f"{mode}.cmd"
            prog.write_text(
                f'@echo off\r\n"{sys.executable}" "{script}" {mode} "{log_dir}" %*\r\nexit /b %ERRORLEVEL%\r\n',
                encoding="utf-8",
                newline="",
            )
        else:
            prog = folder / mode
            write_lf(prog, f'#!/bin/sh\nexec "{sys.executable}" "{script}" {mode} "{log_dir}" "$@"\n')
            prog.chmod(0o755)
        paths[mode] = str(prog)
    return {"MELLON_TECTONIC": paths["tectonic"], "MELLON_DRAWIO": paths["drawio"]}


# ---------------------------------------------------------------------------
# Talking to the server
# ---------------------------------------------------------------------------


class Client:
    """A connected MCP client session, driven from synchronous test code."""

    def __init__(self, portal, session: ClientSession, init: mcp.types.InitializeResult):
        self.portal = portal
        self.session = session
        self.init = init

    async def _call(self, name: str, args: dict) -> mcp.types.CallToolResult:
        return await self.session.call_tool(name, args, read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT))

    def call(self, name: str, args: dict | None = None) -> mcp.types.CallToolResult:
        return self.portal.call(self._call, name, dict(args or {}))

    def tools(self) -> list[mcp.types.Tool]:
        return self.portal.call(self.session.list_tools).tools

    def call_concurrently(self, calls: list[tuple[str, dict]]) -> list[mcp.types.CallToolResult]:
        """Send every call before any answer is awaited."""

        async def run():
            results: list = [None] * len(calls)

            async def one(i: int, name: str, args: dict):
                results[i] = await self._call(name, args)

            async with anyio.create_task_group() as tg:
                for i, (name, args) in enumerate(calls):
                    tg.start_soon(one, i, name, dict(args))
            return results

        return self.portal.call(run)


@contextlib.contextmanager
def mcp_server(args, *, env: dict[str, str], cwd: Path, stderr_path: Path):
    """Start ``mellon-mcp <args>`` over stdio and yield a connected Client."""
    params = StdioServerParameters(command=MELLON_MCP, args=[str(a) for a in args], env=env, cwd=str(cwd))
    with open(stderr_path, "w", encoding="utf-8") as errlog, anyio.from_thread.start_blocking_portal() as portal:
        with portal.wrap_async_context_manager(stdio_client(params, errlog=errlog)) as (read, write):
            session_cm = ClientSession(read, write, read_timeout_seconds=timedelta(seconds=CALL_TIMEOUT))
            with portal.wrap_async_context_manager(session_cm) as session:
                init = portal.call(session.initialize)
                yield Client(portal, session, init)


def payload(res: mcp.types.CallToolResult):
    """The JSON value a tool returned, as the client receives it."""
    sc = res.structuredContent
    if isinstance(sc, dict) and set(sc) == {"result"}:
        return sc["result"]
    texts = [c.text for c in res.content if getattr(c, "type", None) == "text"]
    values = []
    for t in texts:
        try:
            values.append(json.loads(t))
        except ValueError:
            values.append(t)
    return values[0] if len(values) == 1 else values


def result_text(res: mcp.types.CallToolResult) -> str:
    texts = [c.text for c in res.content if getattr(c, "type", None) == "text"]
    return "\n".join(texts) + json.dumps(res.structuredContent or {}, ensure_ascii=False)


def show(res: mcp.types.CallToolResult) -> str:
    return f"isError={res.isError} {result_text(res)[:400]!r}"


def failed(res: mcp.types.CallToolResult) -> bool:
    p = payload(res)
    return bool(res.isError) or (isinstance(p, dict) and p.get("ok") is False)


def succeeded(res: mcp.types.CallToolResult) -> bool:
    p = payload(res)
    return not res.isError and isinstance(p, dict) and p.get("ok") is True


def doc_ids(res: mcp.types.CallToolResult) -> list[str]:
    return sorted(d["doc_id"] for d in payload(res))


def without_roots(value, roots: list[Path]):
    """``value`` with each spelling of the given roots replaced by <ROOT>."""
    if isinstance(value, dict):
        return {k: without_roots(v, roots) for k, v in value.items()}
    if isinstance(value, list):
        return [without_roots(v, roots) for v in value]
    if isinstance(value, str):
        for r in roots:
            for spelling in (str(r), r.as_posix()):
                value = value.replace(spelling, "<ROOT>")
        return value
    return value


INIT_REQUEST = (
    json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": mcp.types.LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "blind-test", "version": "0"},
            },
        }
    )
    + "\n"
).encode()


def run_mcp(args, *, env: dict[str, str], cwd: Path, stdin: bytes = INIT_REQUEST) -> subprocess.CompletedProcess:
    """Run ``mellon-mcp <args>`` to its end, writing ``stdin`` to it."""
    return subprocess.run(
        [MELLON_MCP, *[str(a) for a in args]],
        input=stdin,
        capture_output=True,
        env=env,
        cwd=str(cwd),
        timeout=90,
    )


class RawServer:
    """mellon-mcp on raw pipes, keeping every byte it writes to standard output."""

    def __init__(self, args, *, env: dict[str, str], cwd: Path, stderr_path: Path):
        self._err = open(stderr_path, "wb")
        self.proc = subprocess.Popen(
            [MELLON_MCP, *[str(a) for a in args]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._err,
            env=env,
            cwd=str(cwd),
        )
        self.stdout_lines: list[bytes] = []
        self._lines: queue.Queue = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._next_id = 0

    def _read(self) -> None:
        for line in iter(self.proc.stdout.readline, b""):
            self.stdout_lines.append(line)
            self._lines.put(line)
        self._lines.put(None)

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        rid = self._next_id
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + CALL_TIMEOUT
        while True:
            line = self._lines.get(timeout=max(0.1, deadline - time.monotonic()))
            if line is None:
                raise AssertionError(f"mellon-mcp closed standard output before answering {method}")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("id") == rid:
                return message

    def initialize(self) -> dict:
        answer = self.request(
            "initialize",
            {
                "protocolVersion": mcp.types.LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "blind-test", "version": "0"},
            },
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return answer

    def call_tool(self, name: str, args: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": args})

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self._reader.join(timeout=10)
        self._err.close()


def process_tree(pid: int) -> set[int]:
    """``pid`` and every process descended from it."""
    children: dict[int, list[int]] = {}
    if WINDOWS:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                'Get-CimInstance Win32_Process | ForEach-Object { "$($_.ProcessId) $($_.ParentProcessId)" }',
            ],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
        pairs = [line.split() for line in out.splitlines() if len(line.split()) == 2]
    else:
        pairs = []
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                try:
                    stat = (entry / "stat").read_text()
                except OSError:
                    continue
                pairs.append([entry.name, stat.rsplit(")", 1)[1].split()[1]])
    for child, parent in pairs:
        children.setdefault(int(parent), []).append(int(child))
    tree, todo = set(), [pid]
    while todo:
        p = todo.pop()
        if p not in tree:
            tree.add(p)
            todo.extend(children.get(p, []))
    return tree


def network_sockets(pids: set[int]) -> list[str]:
    """Listening TCP sockets and bound UDP sockets owned by ``pids``."""
    found = []
    if WINDOWS:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, errors="replace").stdout
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 5 and f[0] == "TCP" and f[-1].isdigit() and int(f[-1]) in pids:
                if f[3] == "LISTENING" or f[2] in ("0.0.0.0:0", "[::]:0"):
                    found.append(line.strip())
            elif len(f) >= 4 and f[0] == "UDP" and f[-1].isdigit() and int(f[-1]) in pids:
                found.append(line.strip())
        return found
    inodes = set()
    for pid in pids:
        with contextlib.suppress(OSError):
            for fd in os.listdir(f"/proc/{pid}/fd"):
                with contextlib.suppress(OSError):
                    m = re.fullmatch(r"socket:\[(\d+)\]", os.readlink(f"/proc/{pid}/fd/{fd}"))
                    if m:
                        inodes.add(m.group(1))
    for table in ("tcp", "tcp6", "udp", "udp6"):
        with contextlib.suppress(OSError):
            for line in Path(f"/proc/net/{table}").read_text().splitlines()[1:]:
                f = line.split()
                if len(f) > 9 and f[9] in inodes and (table.startswith("udp") or f[3] == "0A"):
                    found.append(f"{table} {f[1]} state {f[3]}")
    return found


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ident_home(tmp_path_factory) -> Path:
    """A home folder whose .gitconfig holds a test-only git identity."""
    home = tmp_path_factory.mktemp("home")
    write_lf(home / ".gitconfig", "[user]\n\tname = Mellon Blind Test\n\temail = blind@example.invalid\n")
    return home


@pytest.fixture(scope="module")
def neutral_dir(tmp_path_factory, ident_home) -> Path:
    """A working directory outside any repository."""
    d = tmp_path_factory.mktemp("cwd")
    r = subprocess.run(
        ["git", "-C", str(d), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        env=child_env(ident_home),
    )
    assert r.returncode != 0, f"setup: {d} is inside a git repository"
    return d


@pytest.fixture(scope="module")
def plain_template(tmp_path_factory) -> Path:
    """A clean copy of the req-test fixture."""
    _ensure_req_test_fixture()
    dst = tmp_path_factory.mktemp("plain") / "repo"
    shutil.copytree(REQ_TEST, dst)
    _clean_worktree(dst)
    return dst


@pytest.fixture(scope="module")
def rich_template(tmp_path_factory, plain_template, ident_home) -> Path:
    """req-test plus a committed figure on VCU3-SRS-13 and table on VCU3-SRS-16."""
    dst = clone_of(plain_template, tmp_path_factory.mktemp("rich") / "repo")
    env = child_env(ident_home)
    write_lf(dst / "VCU3-SRS" / "figures" / "VCU3-SRS-13.svg", SVG)
    obj = dst / "VCU3-SRS" / "objects" / "VCU3-SRS-16.xml"
    write_lf(obj, obj.read_text(encoding="utf-8").replace("</object>", TABLE_XML + "</object>"))
    git(dst, "add", "-A", env=env)
    git(dst, "commit", "-q", "-m", "Blind tests: add a figure and a table", env=env)
    return dst


def reversed_outline(srv: Client, doc_id: str) -> list:
    shown = payload(srv.call("show_document", {"doc_id": doc_id, "include": ["outline"]}))
    return list(reversed(shown["outline"]))


def append_to_description(path: Path, words: str) -> None:
    text = path.read_text(encoding="utf-8")
    write_lf(path, text.replace("</description>", f" {words}</description>", 1))


# Helpers for the MELLON-MCP-SRS blind tests of the write tools


def _new_id(created: set[str]) -> str:
    for path in sorted(created):
        m = re.fullmatch(r"VCU3-SRS/objects/(VCU3-SRS-\d+)\.xml", path)
        if m:
            return m.group(1)
    raise AssertionError(f"setup: no new object file among {sorted(created)}")


def write_calls(figure_source: Path) -> list[tuple[str, object]]:
    """One valid call of each of the 27 write tools, in an order where each is valid.

    An argument set may be a function of (client, created files by tool) for values
    that come from an earlier call.
    """
    return [
        ("new_object", {"doc_id": "VCU3-SRS", "parent_id": "VCU3-SRS-0", "description": "Blind new object.",
                        "attrs": {"type": "Requirement", "status": "Draft"}}),
        ("set_attribute", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-2", "name": "status", "value": "Reviewed"}),
        ("edit_description", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-2",
                              "text": "The VCU shall sample the coolant temperature at a rate of at least 25 Hz."}),
        ("update_object", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-11", "description": "Updated by a blind test.",
                           "attrs": {"priority": "Should"}}),
        ("copy_object", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-12"}),
        ("bulk_set_attribute", {"doc_id": "VCU3-SRS", "obj_ids": ["VCU3-SRS-12", "VCU3-SRS-15"],
                                "name": "priority", "value": "Could"}),
        ("set_figure", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-2", "source_path": str(figure_source)}),
        ("clear_figure", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-13"}),
        ("set_table", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-17", "table": TABLE}),
        ("clear_table", {"doc_id": "VCU3-SRS", "obj_id": "VCU3-SRS-16"}),
        ("edit_metadata", {"doc_id": "VCU3-SRS", "field": "title", "value": "Blind title"}),
        ("tag_issue", {"doc_id": "VCU3-SRS", "issue": "2"}),
        ("create_document", {"doc_id": "BLIND-DOC", "title": "Blind document"}),
        ("add_parent_doc", {"doc_id": "BLIND-DOC", "parent_doc": "VCU3-SRS"}),
        ("remove_parent_doc", {"doc_id": "BLIND-DOC", "parent_doc": "VCU3-SRS"}),
        ("define_attribute", {"doc_id": "VCU3-SRS", "name": "blind-attr", "kind": "enum", "options": ["A", "B"],
                              "default_value": "A"}),
        ("add_attr_option", {"doc_id": "VCU3-SRS", "attr_name": "blind-attr", "option": "C"}),
        ("remove_attr_option", {"doc_id": "VCU3-SRS", "attr_name": "blind-attr", "option": "A"}),
        ("change_attribute_required", {"doc_id": "VCU3-SRS", "name": "blind-attr", "required": True}),
        ("delete_attribute", {"doc_id": "VCU3-SRS", "name": "blind-attr"}),
        ("manage_view", {"doc_id": "VCU3-SRS", "action": "create", "name": "Blind view", "visible_attrs": ["status"]}),
        ("move_object", lambda srv, created: {"doc_id": "VCU3-SRS", "obj_id": _new_id(created["new_object"]),
                                              "to_top": True}),
        ("reorder_outline", lambda srv, created: {"doc_id": "VCU3-SRS",
                                                  "ordering": reversed_outline(srv, "VCU3-SRS")}),
        ("add_link", {"source_doc": "VCU3-SRS", "source_id": "VCU3-SRS-10", "target_doc": "VCU3-SyRS",
                      "target_id": "VCU3-SyRS-42", "link_type": "satisfies"}),
        ("remove_link", {"source_doc": "VCU3-SRS", "source_id": "VCU3-SRS-10", "target_doc": "VCU3-SyRS",
                         "target_id": "VCU3-SyRS-42"}),
        ("delete_object", lambda srv, created: {"doc_id": "VCU3-SRS", "obj_id": _new_id(created["copy_object"])}),
        ("ack_suspects", {"doc_id": "VCU3-SRS", "link_keys": [
            {"source_id": "VCU3-SRS-15", "target_doc": "VCU3-SyRS", "target_id": "VCU3-SyRS-47"}]}),
    ]


@pytest.fixture(scope="module")
def write_run(tmp_path_factory, rich_template, ident_home, neutral_dir):
    """The 27 write calls on one server, with each file's hash before and after each."""
    base = tmp_path_factory.mktemp("writes")
    root = clone_of(rich_template, base / "repo")
    figure_source = base / "figure.svg"
    write_lf(figure_source, SVG)
    runs: dict[str, dict] = {}
    created: dict[str, set[str]] = {}
    with mcp_server(
        ["--repo-root", root], env=child_env(ident_home), cwd=neutral_dir, stderr_path=base / "stderr.txt"
    ) as srv:
        for tool, args in write_calls(figure_source):
            if callable(args):
                args = args(srv, created)
            before = tree_hashes(root, include_git=False)
            res = srv.call(tool, args)
            after = tree_hashes(root, include_git=False)
            created[tool] = set(after) - set(before)
            runs[tool] = {"res": res, "changed": changed(before, after), "args": args}
    return {"root": root, "runs": runs}
