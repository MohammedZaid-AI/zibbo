"""The ``zibbo`` command-line client.

A thin, dependency-free terminal client for a running gateway's internal API. It is the
bridge between the gateway (the engine) and the editor plugins (the UX): the Claude Code
and Codex plugins shell out to ``zibbo`` rather than re-implementing any of this, so all
the intelligence stays in one place and users get a usable CLI for free.

Stdlib only — ``urllib``, ``argparse``, ``json`` — so it imports even where the gateway's
own dependencies are not installed, and starts instantly. It talks to the gateway over
the same public /internal/* HTTP API the plugins use; it never reaches into gateway
internals.

    zibbo status | stats | doctor | benchmark | enable | disable | logs | version | start
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8000"
_DISCOVERY_PORTS = (8000, 8080, 8123)
_ENV_URL = "ZIBBO_GATEWAY_URL"
_ENV_TOKEN = "ZIBBO_GATEWAY_TOKEN"  # noqa: S105 — an env var name, not a secret

# Decorative glyphs, downgraded to ASCII on a terminal that cannot encode them (a
# legacy Windows console). ``_configure_output`` sets these before anything prints.
_TICK = "✓"
_CROSS = "✗"
_WARN = "!"
_DOT = "·"
_ELLIPSIS = "…"


def _configure_output() -> None:
    """Make Unicode output safe on every terminal.

    Two problems, both real on Windows: a legacy console encoding (cp1252) raises
    ``UnicodeEncodeError`` on a check mark, and even UTF-8 output can mojibake on an
    old code page. So we reconfigure the streams to UTF-8 (belt), and if the *original*
    encoding could not represent our glyphs, downgrade them to ASCII (suspenders).
    """
    global _TICK, _CROSS, _WARN, _DOT, _ELLIPSIS
    original = sys.stdout.encoding or "ascii"
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")
    try:
        "✓✗·…".encode(original)
    except (UnicodeError, LookupError):
        _TICK, _CROSS, _WARN, _DOT, _ELLIPSIS = "[OK]", "[X]", "[!]", "-", "..."


# -- HTTP (stdlib) -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GatewayError(Exception):
    """A reachable gateway is required and was not found, or answered an error."""

    message: str

    def __str__(self) -> str:
        return self.message


class Client:
    """Minimal JSON-over-HTTP client for the internal API."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 3.0) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self._base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 — fixed localhost URL
        request.add_header("accept", "application/json")
        if data is not None:
            request.add_header("content-type", "application/json")
        if self._token:
            request.add_header("authorization", f"Bearer {self._token}")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            raise GatewayError(f"gateway returned {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise GatewayError(f"no gateway at {self._base} ({exc})") from exc

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, body)

    def reachable(self) -> bool:
        try:
            self.get("/internal/version")
        except GatewayError:
            return False
        return True


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read() or b"{}")
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            message = payload["error"].get("message")
            if isinstance(message, str):
                return message
    except (ValueError, OSError):
        pass
    return exc.reason if isinstance(exc.reason, str) else "error"


def discover(explicit: str | None, token: str | None) -> Client:
    """Find a reachable gateway: an explicit URL, then $ZIBBO_GATEWAY_URL, then probe.

    Discovery rather than assumption — the spec's requirement. A returned client is not
    guaranteed reachable when an explicit URL was given (we honour the user's choice),
    but the probed fallbacks are only returned when they answer.
    """
    if explicit:
        return Client(explicit, token=token)
    env = os.environ.get(_ENV_URL)
    if env:
        return Client(env, token=token)
    for port in _DISCOVERY_PORTS:
        candidate = Client(f"http://127.0.0.1:{port}", token=token)
        if candidate.reachable():
            return candidate
    return Client(DEFAULT_URL, token=token)


# -- Rendering (pure; unit-tested without a server) --------------------------


def render_status(data: dict[str, Any]) -> str:
    lines = [f"Zibbo Gateway  ({data['name']} {data['version']}, {data['environment']})", ""]
    lines.append(_flag(True, "Gateway running"))
    lines.append(_flag(data["cache_enabled"], f"Cache enabled ({data['cache_backend']})"))
    lines.append(_flag(data["pipeline_active"], "Transformation pipeline active"))
    lines.append(_flag(data["documents_enabled"], "Document extraction enabled"))
    for provider in data.get("providers", []):
        lines.append(_flag(True, f"{provider['name']} provider detected  ({provider['prefix']})"))
    transformers = ", ".join(data.get("transformers", [])) or "none"
    lines.append("")
    lines.append(f"  transformers: {transformers}")
    lines.append(f"  uptime: {_duration(data['uptime_seconds'])}")
    return "\n".join(lines)


def render_stats(data: dict[str, Any]) -> str:
    today = data["today"]
    all_time = data["all_time"]
    out = [f"Zibbo stats  (today {data['date']} {_DOT} all-time)", ""]
    out.append(_stat_row("Requests", today["requests"], all_time["requests"]))
    out.append(_stat_row("Optimized", today["optimized"], all_time["optimized"]))
    out.append(_stat_row("Transformations", today["transformations"], all_time["transformations"]))
    out.append(
        _stat_row(
            "Tokens saved",
            f"{today['tokens_saved']:,}",
            f"{all_time['tokens_saved']:,}",
        )
    )
    out.append(
        _stat_row(
            "Token reduction",
            f"{today['token_reduction_pct']}%",
            f"{all_time['token_reduction_pct']}%",
        )
    )
    out.append(
        _stat_row(
            "Cache hit rate",
            f"{round(today['cache_hit_rate'] * 100, 1)}%",
            f"{round(all_time['cache_hit_rate'] * 100, 1)}%",
        )
    )
    out.append(
        _stat_row("Avg latency", f"{today['avg_latency_ms']}ms", f"{all_time['avg_latency_ms']}ms")
    )
    top = today.get("top_transformer") or all_time.get("top_transformer")
    out.append(f"  Top transformer: {top['name'] if top else 'n/a'}")
    cost = today.get("estimated_cost_usd")
    if cost is not None:
        out.append(f"  Estimated savings today: ${cost}")
    else:
        out.append("  Estimated savings: set ZIBBO_ANALYTICS_COST_PER_MILLION_TOKENS to estimate $")
    return "\n".join(out)


def render_benchmark(data: dict[str, Any]) -> str:
    if data["source"] == "none":
        return data.get("note") or "Nothing to benchmark yet."
    header = "Zibbo benchmark  " + (
        "(sample)" if data["source"] == "provided" else "(last request, replayed from metadata)"
    )
    transformers = ", ".join(data.get("transformers", [])) or "none"
    lines = [
        header,
        "",
        f"  Content type:     {data.get('content_type') or 'n/a'}",
        f"  Original tokens:  {data['original_tokens']:,}",
        f"  Optimized tokens: {data['optimized_tokens']:,}",
        f"  Reduction:        {data['reduction_pct']}%",
        f"  Transformers:     {transformers}",
        f"  Cache used:       {'yes' if data['cache_used'] else 'no'}",
        f"  Processing time:  {data['processing_time_ms']}ms",
    ]
    if data.get("note"):
        lines += ["", f"  {data['note']}"]
    return "\n".join(lines)


def render_doctor(data: dict[str, Any]) -> str:
    glyph = {"ok": _TICK, "warn": _WARN, "fail": _CROSS}
    lines = ["Zibbo doctor", ""]
    for check in data["checks"]:
        lines.append(f"  {glyph.get(check['status'], '?')} {check['name']}: {check['detail']}")
        if check.get("fix"):
            lines.append(f"      fix: {check['fix']}")
    lines.append("")
    lines.append("All systems go." if data["healthy"] else "Problems found — see fixes above.")
    return "\n".join(lines)


def render_logs(data: dict[str, Any]) -> str:
    if not data["events"]:
        return "No optimization activity recorded yet."
    lines = [f"Zibbo recent activity ({data['count']})", ""]
    for event in data["events"]:
        stamp = time.strftime("%H:%M:%S", time.localtime(event["timestamp"]))
        if event["applied"]:
            kinds = ",".join(event["content_types"]) or "?"
            cache = f" cache={event['cache_status']}" if event["cache_status"] else ""
            lines.append(
                f"  {stamp}  {event['endpoint']}  {kinds}  "
                f"-{event['tokens_saved']:,} tok  {event['execution_time_ms']}ms{cache}"
            )
        else:
            lines.append(f"  {stamp}  {event['endpoint']}  skipped ({event['skip_reason']})")
    return "\n".join(lines)


def _flag(ok: bool, label: str) -> str:
    return f"  {_TICK if ok else _CROSS} {label}"


def _stat_row(label: str, today: object, all_time: object) -> str:
    return f"  {label:<18} {today!s:>12}   {all_time!s:>14}"


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


# -- Commands ----------------------------------------------------------------


def _cmd_status(client: Client, _args: argparse.Namespace) -> int:
    print(render_status(client.get("/internal/status")))
    return 0


def _cmd_stats(client: Client, _args: argparse.Namespace) -> int:
    print(render_stats(client.get("/internal/stats")))
    return 0


def _cmd_doctor(client: Client, _args: argparse.Namespace) -> int:
    data = client.post("/internal/doctor")
    print(render_doctor(data))
    return 0 if data["healthy"] else 1


def _cmd_benchmark(client: Client, args: argparse.Namespace) -> int:
    content: str | None = args.content
    if args.file:
        with open(args.file, encoding="utf-8", errors="replace") as handle:
            content = handle.read()
    body: dict[str, Any] = {}
    if content is not None:
        body["content"] = content
    if args.model:
        body["model"] = args.model
    print(render_benchmark(client.post("/internal/benchmark", body)))
    return 0


def _cmd_enable(client: Client, _args: argparse.Namespace) -> int:
    data = client.post("/internal/enable")
    print(f"Optimization {'enabled' if data['optimization_enabled'] else 'disabled'}.")
    return 0


def _cmd_disable(client: Client, _args: argparse.Namespace) -> int:
    data = client.post("/internal/disable")
    print(f"Optimization {'enabled' if data['optimization_enabled'] else 'disabled'}.")
    return 0


def _cmd_logs(client: Client, args: argparse.Namespace) -> int:
    print(render_logs(client.get(f"/internal/logs?limit={args.limit}")))
    return 0


def _cmd_version(client: Client, _args: argparse.Namespace) -> int:
    data = client.get("/internal/version")
    api = data["internal_api_version"]
    print(f"{data['app_name']} {data['gateway_version']} (internal API v{api})")
    return 0


def _cmd_start(client: Client, args: argparse.Namespace) -> int:
    """Start the gateway if it is not already running, then wait for it to answer."""
    if client.reachable():
        print(f"{_TICK} Zibbo already running at {client.base_url}")
        return 0
    try:
        import gateway  # noqa: F401 — verify the package is importable before spawning
    except ImportError:
        print(
            f"{_CROSS} Zibbo gateway is not installed.\n"
            "    Install it with:  pip install zibbo\n"
            "    Then run:         zibbo start",
            file=sys.stderr,
        )
        return 2

    import subprocess

    env = dict(os.environ)
    if args.port:
        env["ZIBBO_PORT"] = str(args.port)
    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    else:
        start_new_session = True
    subprocess.Popen(
        [sys.executable, "-m", "gateway"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=start_new_session,
    )
    print(f"Starting Zibbo gateway{_ELLIPSIS}", file=sys.stderr)
    for _ in range(50):  # up to ~10s
        time.sleep(0.2)
        if client.reachable():
            print(f"{_TICK} Zibbo running at {client.base_url}")
            return 0
    print(f"{_CROSS} gateway did not become ready in time", file=sys.stderr)
    return 1


_COMMANDS = {
    "status": _cmd_status,
    "stats": _cmd_stats,
    "doctor": _cmd_doctor,
    "benchmark": _cmd_benchmark,
    "enable": _cmd_enable,
    "disable": _cmd_disable,
    "logs": _cmd_logs,
    "version": _cmd_version,
    "start": _cmd_start,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zibbo", description="Talk to a running Zibbo gateway.")
    parser.add_argument("--url", help=f"gateway base URL (default: discover, then {DEFAULT_URL})")
    parser.add_argument("--token", help="bearer token for a remote-enabled internal API")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="gateway status banner")
    sub.add_parser("stats", help="optimization statistics")
    sub.add_parser("doctor", help="run diagnostics")
    sub.add_parser("enable", help="enable transformations")
    sub.add_parser("disable", help="disable transformations")
    sub.add_parser("version", help="gateway and API version")

    bench = sub.add_parser("benchmark", help="replay through the pipeline (no upstream call)")
    bench.add_argument(
        "--content", help="sample text to benchmark; omit to replay the last request"
    )
    bench.add_argument("--file", help="read the sample from a file instead")
    bench.add_argument("--model", help="tokenizer model to count against")

    logs = sub.add_parser("logs", help="recent optimization activity (metadata only)")
    logs.add_argument("--limit", type=int, default=20)

    start = sub.add_parser("start", help="start the gateway if it is not already running")
    start.add_argument("--port", type=int, help="port to start on (sets ZIBBO_PORT)")

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    args = build_parser().parse_args(argv)
    token = args.token or os.environ.get(_ENV_TOKEN)
    client = discover(args.url, token)
    handler = _COMMANDS[args.command]
    try:
        return handler(client, args)
    except GatewayError as exc:
        print(f"{_CROSS} {exc}", file=sys.stderr)
        print("    Is the gateway running?  Try:  zibbo start", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
