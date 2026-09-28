# -*- coding: utf-8 -*-
"""Talk to a running Unreal Editor through the VibeUE MCP server -- without the
AI client's MCP connection.

    python tools/ue_bridge.py script.py          # run a script inside the editor
    python tools/ue_bridge.py -a script.py       # run it from the editor TICK (long jobs)
    python tools/ue_bridge.py -c "print(1+1)"    # run one line
    python tools/ue_bridge.py -t 900 script.py   # custom timeout (default 600 s)

WHY THIS EXISTS
---------------
VibeUE is a plain streamable-HTTP MCP server at 127.0.0.1:8000/mcp, living
inside UnrealEditor.exe. An AI client (Claude Code, Cursor, ...) connects to it
ONCE, when its session starts. If the editor crashes or restarts, the client's
tools disappear for the rest of the session -- but the server is right there,
listening again. This script does the MCP handshake by hand and calls
`execute_python_code`, so work continues without restarting the AI session.

It also fixes the timeout problem: `execute_python_code` gives up REPORTING
after ~30 s on the server side, while the code keeps running. A 4-minute level
build comes back as "timeout" whether it succeeded or failed. Mode `-a` runs
the script from a slate post-tick callback (top of the main loop, like clicking
a UI button) and writes the full output to a log file with an end marker and
return code.

Running from the tick also avoids a hard crash: `execute_python_code` runs
inside a TaskGraph task, and heavy asset operations (FBX import, static mesh
build) that flush the task graph from there hit
`Assertion failed: ++Queue(QueueIndex).RecursionGuard == 1` and take the editor
down.

Env vars:
    UE_BRIDGE_URL   MCP endpoint (default http://127.0.0.1:8000/mcp)
    UE_BRIDGE_LOG   output file for -a mode (use one per agent/session if
                    several agents share the same editor)

Stdlib only. Tested with Unreal Engine 5.8 + VibeUE.
"""
import json
import os
import sys
import urllib.request

URL = os.environ.get("UE_BRIDGE_URL", "http://127.0.0.1:8000/mcp")
HEADERS = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream"}


def post(body, session=None, timeout=600):
    headers = dict(HEADERS)
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(URL, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.headers.get("Mcp-Session-Id"), r.read().decode("utf-8", "replace")


def parse(raw):
    """The reply may be plain JSON or SSE (`data: {...}`)."""
    raw = raw.strip()
    if raw.startswith("data:"):
        raw = "\n".join(l[5:].strip() for l in raw.splitlines() if l.startswith("data:"))
    return json.loads(raw)


def open_session():
    """MCP handshake; returns the session id."""
    session, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05",
                                  "capabilities": {},
                                  "clientInfo": {"name": "ue_bridge.py", "version": "1"}}},
                      timeout=30)
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session, 30)
    return session


class ToolError(Exception):
    pass


def call_toolset(session, toolset, name, args, timeout=120):
    """Call an engine TOOLSET tool through VibeUE's `call_tool` gateway and
    return the decoded `returnValue`. Useful for engine toolsets without a
    friendly Python API (e.g. NiagaraToolsets.NiagaraToolset_System)."""
    _, raw = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "call_tool",
                              "arguments": {"toolset_name": toolset,
                                            "tool_name": name,
                                            "arguments": args}}},
                  session, timeout)
    d = parse(raw)
    if "error" in d:
        raise ToolError("%s: %s" % (name, json.dumps(d["error"], ensure_ascii=False)))
    res = d.get("result", {})
    text = "".join(p.get("text", "") for p in res.get("content", []))
    if res.get("isError"):
        raise ToolError("%s: %s" % (name, text[:2000]))
    try:
        r = json.loads(text)
    except ValueError:
        raise ToolError("%s: reply is not JSON: %s" % (name, text[:2000]))
    return r.get("returnValue", r) if isinstance(r, dict) else r


def run(code, timeout=600):
    session = open_session()
    _, raw = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                   "params": {"name": "execute_python_code",
                              "arguments": {"code": code}}}, session, timeout)
    d = parse(raw)
    if "error" in d:
        print("SERVER ERROR:", json.dumps(d["error"], ensure_ascii=False))
        return 1
    for part in d.get("result", {}).get("content", []):
        text = part.get("text", "")
        try:
            r = json.loads(text)
        except ValueError:
            print(text)
            continue
        if r.get("output"):
            print(r["output"])
        if not r.get("success", True):
            print("FAILED: %s\n%s" % (r.get("error_code", ""), r.get("error_message", "")))
            return 1
    return 0


LOG = os.environ.get("UE_BRIDGE_LOG") or os.path.join(
    os.environ.get("TEMP", os.environ.get("TMPDIR", ".")), "ue_bridge_output.txt")

# Runs the file from the editor's slate post-tick callback; output goes to LOG
# followed by "<<<END rc=N>>>". A global flag prevents two tick jobs at once.
LAUNCHER = u"""
import unreal, sys, io, traceback
_g = globals()
if _g.get("_ue_bridge_running"):
    print("A SCRIPT IS ALREADY RUNNING -- wait for it to finish")
else:
    _g["_ue_bridge_running"] = True
    _g["_ue_bridge"] = {"h": None}
    def _step(delta):
        st = _g["_ue_bridge"]
        unreal.unregister_slate_post_tick_callback(st["h"])
        buf = io.StringIO()
        old = sys.stdout
        rc = 0
        try:
            sys.stdout = buf
            _p = %(path)r
            _f = open(_p, "rb").read().decode("utf-8")
            exec(compile(_f, _p, "exec"), {"__name__": "__main__"})
        except Exception:
            rc = 1
            buf.write("\\n" + traceback.format_exc())
        finally:
            sys.stdout = old
            _g["_ue_bridge_running"] = False
            with open(%(log)r, "wb") as f:
                f.write(buf.getvalue().encode("utf-8", "replace"))
                f.write(("\\n<<<END rc=%%d>>>\\n" %% rc).encode("utf-8"))
    _g["_ue_bridge"]["h"] = unreal.register_slate_post_tick_callback(_step)
    print("LAUNCHED")
"""


def launch(path):
    try:
        os.remove(LOG)
    except OSError:
        pass
    return run(LAUNCHER % {"path": os.path.abspath(path), "log": LOG}, timeout=30)


def main():
    args = sys.argv[1:]
    timeout = 600
    if args and args[0] == "-t":
        timeout = int(args[1])
        args = args[2:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "-a":
        print("log:", LOG)
        return launch(args[1])
    if args[0] == "-c":
        code = args[1]
    else:
        # Send only the PATH and let the editor open the file. VibeUE scans the
        # code text it receives and refuses some patterns (e.g.
        # `get_default_object(...)`); a file executed by path behaves exactly
        # like the editor's Python console, and tracebacks show the real path.
        path = os.path.abspath(args[0])
        code = ("import warnings\nwarnings.simplefilter('ignore')\n"
                "_p = %r\n"
                "_f = open(_p, 'rb').read().decode('utf-8')\n"
                "exec(compile(_f, _p, 'exec'), {'__name__': '__main__'})" % path)
    return run(code, timeout)


if __name__ == "__main__":
    sys.exit(main())
