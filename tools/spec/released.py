"""Call tools on a *released* Mellon MCP server, to edit Mellon's own specification.

docs/specification.md: Mellon's own documents are edited with a released build, never
with the development build in this repository, so that a bug being worked on can't damage
the specification. This script finds the released server, starts it on this repository
and makes each call in turn.

Usage, from the repository root, with the development environment's Python (it has the
`mcp` client package):

    uv run --project mellon python tools/spec/released.py calls.json [--stop-on-error]
    echo '[{"tool": "list_documents"}]' | uv run --project mellon python tools/spec/released.py -

calls.json is a list of {"tool": <name>, "args": {...}}. One JSON line is printed per call.
The exit status is 1 when a call failed (an error, or "ok": false).

The released server is, in this order:
  - $MELLON_RELEASED_MCP,
  - on Windows, the installer's %LOCALAPPDATA%\\Programs\\Mellon\\mellon-mcp.exe,
  - elsewhere, /opt/mellon-release/bin/mellon-mcp, which tools/cloud-setup.sh installs
    from the release tag.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]


def released_server() -> str:
    if os.environ.get("MELLON_RELEASED_MCP"):
        return os.environ["MELLON_RELEASED_MCP"]
    if sys.platform == "win32":
        exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Mellon" / "mellon-mcp.exe"
    else:
        exe = Path("/opt/mellon-release/bin/mellon-mcp")
    if not exe.is_file():
        sys.exit(f"no released Mellon MCP server at {exe}; install one (tools/cloud-setup.sh) "
                 "or set MELLON_RELEASED_MCP")
    return str(exe)


async def run(calls: list[dict], stop_on_error: bool) -> bool:
    server = released_server()
    params = StdioServerParameters(command=server, args=["--repo-root", str(ROOT)])
    failed = False
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            info = await s.initialize()
            print(json.dumps({"server": server, "version": info.serverInfo.version}), file=sys.stderr)
            for call in calls:
                res = await s.call_tool(call["tool"], call.get("args", {}))
                text = "".join(c.text for c in res.content if getattr(c, "text", None))
                try:
                    data = json.loads(text)
                except ValueError:
                    data = text
                bad = res.isError or (isinstance(data, dict) and data.get("ok") is False)
                print(json.dumps({"tool": call["tool"], "error": bad, "result": data}, ensure_ascii=False))
                if bad:
                    failed = True
                    if stop_on_error:
                        break
    return failed


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = sys.argv[1]
    calls = json.load(sys.stdin if src == "-" else open(src, encoding="utf-8"))
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(1 if asyncio.run(run(calls, "--stop-on-error" in sys.argv)) else 0)


if __name__ == "__main__":
    main()
