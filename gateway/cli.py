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

import gateway
from gateway.claude_env import detect_auth, detect_routing, read_claude_settings

_PLUGIN_VERSION = gateway.__version__

DEFAULT_URL = "http://127.0.0.1:8000"
_DISCOVERY_PORTS = (8000, 8080, 8123)
_ENV_URL = "ZIBBO_GATEWAY_URL"
_ENV_TOKEN = "ZIBBO_GATEWAY_TOKEN"  # noqa: S105 — an env var name, not a secret
_ENV_DEBUG = "ZIBBO_DEBUG"

# The one supported way to install the CLI so that editors (Claude Code / Codex) can see
# it. The package is not on PyPI yet, so this points at the repo rather than `pip install
# zibbo`. pipx puts `zibbo` on PATH globally, which is what a plugin shell needs.
_INSTALL_HINT = "pipx install git+https://github.com/MohammedZaid-AI/zibbo"


def _debug(message: str) -> None:
    """Trace to stderr when ZIBBO_DEBUG is set. The plugins forward this to the user."""
    if os.environ.get(_ENV_DEBUG):
        print(f"[zibbo:debug] {message}", file=sys.stderr)


# Decorative glyphs, downgraded to ASCII on a terminal that cannot encode them (a
# legacy Windows console). ``_configure_output`` sets these before anything prints.
_TICK = "✓"
_CROSS = "✗"
_WARN = "!"
_DOT = "·"
_ELLIPSIS = "…"
# Activation-state lamps for the banner. Downgrade to a text tag so the header still reads.
_GREEN = "🟢"
_YELLOW = "🟡"
_RED = "🔴"


def _configure_output() -> None:
    """Make Unicode output safe on every terminal.

    Two problems, both real on Windows: a legacy console encoding (cp1252) raises
    ``UnicodeEncodeError`` on a check mark, and even UTF-8 output can mojibake on an
    old code page. So we reconfigure the streams to UTF-8 (belt), and if the *original*
    encoding could not represent our glyphs, downgrade them to ASCII (suspenders).
    """
    global _TICK, _CROSS, _WARN, _DOT, _ELLIPSIS, _GREEN, _YELLOW, _RED
    original = sys.stdout.encoding or "ascii"
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")
    try:
        "✓✗·…🟢🟡🔴".encode(original)
    except (UnicodeError, LookupError):
        _TICK, _CROSS, _WARN, _DOT, _ELLIPSIS = "[OK]", "[X]", "[!]", "-", "..."
        _GREEN, _YELLOW, _RED = "[+]", "[!]", "[x]"


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
        _debug(f"{method} {url}")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                _debug(f"{method} {url} -> {response.status}")
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            _debug(f"{method} {url} -> HTTP {exc.code}: {detail}")
            raise GatewayError(f"gateway returned {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            _debug(f"{method} {url} -> unreachable: {exc}")
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
        _debug(f"discovery: using explicit --url {explicit}")
        return Client(explicit, token=token)
    env = os.environ.get(_ENV_URL)
    if env:
        _debug(f"discovery: using ${_ENV_URL} {env}")
        return Client(env, token=token)
    for port in _DISCOVERY_PORTS:
        candidate = Client(f"http://127.0.0.1:{port}", token=token)
        _debug(f"discovery: probing {candidate.base_url}")
        if candidate.reachable():
            _debug(f"discovery: reachable at {candidate.base_url}")
            return candidate
    _debug(f"discovery: no gateway found on {_DISCOVERY_PORTS}; defaulting to {DEFAULT_URL}")
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


def _routing_help(auth: dict[str, Any], routing: dict[str, Any]) -> list[str]:
    """The one honest instruction for turning routing on. Never mentions API keys.

    Item 6 of the spec: an authenticated subscription user's only missing step is routing,
    and telling them to make an API key would be wrong.
    """
    out = [""]
    if auth["present"]:
        out.append("  Claude Code is authenticated correctly.")
        out.append("  The only missing step is routing. Run:")
    else:
        out.append("  Route Claude Code through Zibbo. Run:")
    out += [
        "",
        f"    export ANTHROPIC_BASE_URL={routing['expected_base_url']}",
        "",
        "  Then restart Claude Code.",
    ]
    return out


def _kv_flag(ok: bool, key: str, value: str) -> str:
    return f"  {_TICK if ok else _CROSS} {key:<15}{value}"


def render_banner(banner: dict[str, Any]) -> str:
    """The activation banner shown at every Claude Code startup (spec item 3)."""
    gateway = banner.get("gateway")
    if gateway is None or not gateway.get("running"):
        return "\n".join(
            [
                f"{_RED} Zibbo - gateway not running".strip(),
                "",
                _flag(False, "Gateway not reachable"),
                "",
                "  Start it:  zibbo start",
            ]
        )

    auth = banner["auth"]
    routing = banner["routing"]
    cache = banner["cache"]
    routed = routing["routed"]
    optimizing = banner["optimization_enabled"]
    lamp = _GREEN if routed else _YELLOW
    headline = "Zibbo Active" if routed else "Zibbo - one step left"
    lines = [
        f"{lamp} {headline}".strip(),
        "",
        _kv_flag(True, "Gateway", f"Running ({gateway['version']})"),
        _kv_flag(auth["present"], "Authentication", auth["label"]),
        _kv_flag(routed, "Routing", "Through Zibbo" if routed else "Not routed"),
        _kv_flag(optimizing, "Optimization", "Enabled" if optimizing else "Disabled"),
        _kv_flag(
            cache["enabled"], "Cache", f"Ready ({cache['backend']})" if cache["enabled"] else "Off"
        ),
    ]
    if routed:
        lines += ["", "  Type  /zibbo  for details."]
    else:
        lines += _routing_help(auth, routing)
    return "\n".join(lines)


def render_dashboard(dash: dict[str, Any]) -> str:
    """The friendly ``/zibbo`` / ``zibbo status`` dashboard (spec items 4 and 8)."""
    gateway = dash["gateway"]
    stats = dash["stats"]
    auth = dash["auth"]
    routing = dash["routing"]
    cache = dash["cache"]
    cost = stats.get("estimated_cost_usd")
    savings = f"${cost}" if cost is not None else "set ZIBBO_ANALYTICS_COST_PER_MILLION_TOKENS"
    rows = [
        ("Gateway", f"Running ({gateway['version']}, {gateway['environment']})"),
        ("Provider", dash["provider"]),
        ("Authentication", auth["label"]),
        ("Routing", "Through Zibbo" if routing["routed"] else "Not routed"),
        ("Requests today", f"{stats['requests']:,}"),
        ("Average reduction", f"{stats['token_reduction_pct']}%"),
        ("Cache hit rate", f"{round(stats['cache_hit_rate'] * 100, 1)}%"),
        ("Estimated savings", savings),
        ("Optimization", "Enabled" if dash["optimization_enabled"] else "Disabled"),
        ("Cache", f"Ready ({cache['backend']})" if cache["enabled"] else "Off"),
    ]
    lines = ["Zibbo", ""]
    lines += [f"  {label:<20}{value}" for label, value in rows]
    lines += ["", f"  {'Status':<20}{'Healthy' if dash['healthy'] else 'Needs attention'}"]
    if not routing["routed"]:
        lines += _routing_help(auth, routing)
    return "\n".join(lines)


def render_claude(data: dict[str, Any]) -> str:
    """Render the raw ``/internal/claude`` view (``zibbo claude``)."""
    auth = data["authentication"]
    route = data["anthropic_route"] or "not mounted"
    return "\n".join(
        [
            "Zibbo - Claude Code activation (gateway view)",
            "",
            _flag(data["gateway_running"], f"Gateway running ({data['gateway_version']})"),
            _flag(data["optimization_enabled"], "Optimization enabled"),
            _flag(data["cache_enabled"], f"Cache ({data['cache_backend']})"),
            _flag(auth["present"], f"Authentication (gateway env): {auth['label']}"),
            f"  {_DOT} Anthropic route: {route}",
            _flag(data["routing_observed"], "Anthropic traffic observed this run"),
            "",
            "  Authentication and routing are detected authoritatively by the CLI, inside",
            "  Claude Code's own environment. This is the gateway's best-effort view.",
        ]
    )


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
    """Render diagnostics. Every non-ok check explains problem, reason, and exact fix."""
    glyph = {"ok": _TICK, "warn": _WARN, "fail": _CROSS}
    lines = ["Zibbo doctor", ""]
    for check in data["checks"]:
        lines.append(f"  {glyph.get(check['status'], '?')} {check['name']}: {check['detail']}")
        if check["status"] != "ok":
            problem = check.get("problem")
            reason = check.get("reason")
            fix = check.get("fix")
            if problem:
                lines.append(f"      problem: {problem}")
            if reason:
                lines.append(f"      reason:  {reason}")
            if fix:
                lines.append(f"      fix:     {fix}")
    statuses = {check["status"] for check in data["checks"]}
    lines.append("")
    if "fail" in statuses:
        lines.append("Problems found — see fixes above.")
    elif "warn" in statuses:
        lines.append("Working, with warnings — see fixes above.")
    else:
        lines.append("All systems go.")
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


# Friendly labels for the pipeline's internal step names. A step without an entry is
# prettified generically (underscores to spaces, sentence case, with known acronyms).
_STEP_LABELS = {
    "removed_scripts": "Removed scripts",
    "removed_styles": "Removed styles",
    "removed_svg": "Removed SVG graphics",
    "removed_embedded_media": "Removed embedded media",
    "removed_navigation": "Removed navigation",
    "removed_ads_and_banners": "Removed ads and cookie banners",
    "removed_hidden_elements": "Removed hidden elements",
    "converted_to_markdown": "Converted HTML to Markdown",
    "preserved_document_title": "Preserved document title",
    "dropped_data_uri_images": "Dropped data-URI images",
    "minified_json": "Minified JSON",
    "removed_empty_containers": "Removed empty containers",
    "collapsed_duplicate_keys": "Collapsed duplicate keys",
    "normalized_line_endings": "Normalized line endings",
    "stripped_trailing_whitespace": "Stripped trailing whitespace",
    "collapsed_blank_lines": "Collapsed blank lines",
    "collapsed_inline_whitespace": "Collapsed duplicated whitespace",
    "removed_duplicate_paragraphs": "Removed duplicated paragraphs",
    "extracted_document": "Extracted document to Markdown",
}
_ACRONYMS = {
    "html": "HTML",
    "json": "JSON",
    "csv": "CSV",
    "xml": "XML",
    "pdf": "PDF",
    "docx": "DOCX",
}


def humanize_step(step: str) -> str:
    if step in _STEP_LABELS:
        return _STEP_LABELS[step]
    if step.startswith("format_"):
        fmt = step[len("format_") :]
        return f"Extracted {_ACRONYMS.get(fmt, fmt.upper())}"
    words = step.replace("_", " ").split()
    if not words:
        return step
    rendered = [_ACRONYMS.get(word, word) for word in words]
    first = rendered[0]
    rendered[0] = first if first.isupper() else first.capitalize()
    return " ".join(rendered)


def render_explain(data: dict[str, Any]) -> str:
    events = data.get("events", [])
    if not events:
        return "No requests optimized yet. Route your assistant through Zibbo, then try again."
    event = events[0]
    if not event["applied"]:
        return (
            "Last request\n\n"
            f"  Endpoint:  {event['endpoint']}\n"
            f"  Result:    not optimized ({event['skip_reason']})\n"
            "  No transformations were applied to this request."
        )
    before, after = event["tokens_before"], event["tokens_after"]
    saved = event["tokens_saved"]
    pct = round(saved / before * 100, 1) if before else 0.0
    kinds = ", ".join(k.upper() for k in event["content_types"]) or "text"
    lines = [
        "Last request",
        "",
        f"  Content type:      {kinds}",
        f"  Original tokens:   {before:,}",
        f"  Optimized tokens:  {after:,}",
        f"  Saved:             {saved:,} ({pct}%)",
        "",
        "  Transformations applied:",
    ]
    lines.extend(f"    {_TICK} {humanize_step(step)}" for step in event["steps"])
    if not event["steps"]:
        lines.append("    (none — content was already optimal)")
    cache = (event["cache_status"] or "n/a").upper()
    lines += [
        "",
        f"  Cache:             {cache}",
        f"  Transformation:    {event['execution_time_ms']} ms",
    ]
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


def _auth_view() -> dict[str, Any]:
    """Detect Claude authentication from this process's environment (Claude Code's env)."""
    info = detect_auth(os.environ, read_claude_settings())
    return {
        "present": info.present,
        "label": info.label,
        "detail": info.detail,
        "method": info.method,
        "is_api_key": info.is_api_key,
    }


def _routing_view(gateway_base_url: str) -> dict[str, Any]:
    info = detect_routing(os.environ, gateway_base_url)
    return {
        "routed": info.routed,
        "base_url": info.base_url,
        "expected_base_url": info.expected_base_url,
        "reason": info.reason,
    }


def _status_or_none(client: Client) -> dict[str, Any] | None:
    """Fetch /internal/status, or None when the gateway is not reachable."""
    try:
        data: dict[str, Any] = client.get("/internal/status")
    except GatewayError:
        return None
    return data


def _cmd_status(client: Client, _args: argparse.Namespace) -> int:
    status = _status_or_none(client)
    auth = _auth_view()
    routing = _routing_view(client.base_url)
    if status is None:
        print(render_banner({"gateway": None, "auth": auth, "routing": routing}))
        return 1
    stats_today: dict[str, Any] = client.get("/internal/stats")["today"]
    providers = status.get("providers", [])
    provider = (
        "Anthropic" if routing["routed"] else (", ".join(p["name"] for p in providers) or "none")
    )
    dash = {
        "gateway": {"version": status["version"], "environment": status["environment"]},
        "provider": provider,
        "auth": auth,
        "routing": routing,
        "stats": stats_today,
        "optimization_enabled": status["optimization_enabled"],
        "cache": {"enabled": status["cache_enabled"], "backend": status["cache_backend"]},
        "healthy": bool(status["optimization_enabled"] and routing["routed"]),
    }
    print(render_dashboard(dash))
    return 0


def _cmd_banner(client: Client, _args: argparse.Namespace) -> int:
    status = _status_or_none(client)
    auth = _auth_view()
    routing = _routing_view(client.base_url)
    if status is None:
        print(render_banner({"gateway": None, "auth": auth, "routing": routing}))
        return 1
    print(
        render_banner(
            {
                "gateway": {"running": True, "version": status["version"]},
                "auth": auth,
                "routing": routing,
                "optimization_enabled": status["optimization_enabled"],
                "cache": {"enabled": status["cache_enabled"], "backend": status["cache_backend"]},
            }
        )
    )
    return 0


def _cmd_claude(client: Client, _args: argparse.Namespace) -> int:
    print(render_claude(client.get("/internal/claude")))
    return 0


def _cmd_stats(client: Client, _args: argparse.Namespace) -> int:
    print(render_stats(client.get("/internal/stats")))
    return 0


def _check(
    name: str,
    status: str,
    detail: str,
    *,
    problem: str | None = None,
    reason: str | None = None,
    fix: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "problem": problem,
        "reason": reason,
        "fix": fix,
    }


def _build_doctor_checks(client: Client) -> list[dict[str, Any]]:
    """Compose client-side (auth, routing, env) and gateway-side checks into one report.

    Client-side checks are authoritative — they read Claude Code's own environment — and
    run even when the gateway is down. Gateway-side checks are added when reachable.
    """
    checks: list[dict[str, Any]] = []

    reachable = client.reachable()
    checks.append(
        _check(
            "connectivity",
            "ok" if reachable else "fail",
            f"gateway reachable at {client.base_url}"
            if reachable
            else f"no gateway at {client.base_url}",
            problem=None if reachable else "the CLI cannot reach the gateway",
            reason=None if reachable else "nothing is listening on the gateway URL",
            fix=None if reachable else "run  zibbo start  (or set ZIBBO_GATEWAY_URL)",
        )
    )

    status = _status_or_none(client) if reachable else None
    claude = None
    if reachable:
        try:
            claude = client.get("/internal/claude")
        except GatewayError:
            claude = None

    if status is not None:
        checks.append(
            _check("gateway", "ok", f"running {status['version']} ({status['environment']})")
        )
        opt = status["optimization_enabled"]
        checks.append(
            _check(
                "optimization",
                "ok" if opt else "warn",
                "enabled" if opt else "disabled",
                problem=None if opt else "optimization is switched off",
                reason=None if opt else "the runtime kill switch is off",
                fix=None if opt else "run  zibbo enable",
            )
        )
        cache_on = status["cache_enabled"]
        checks.append(
            _check(
                "cache",
                "ok" if cache_on else "warn",
                f"{status['cache_backend']} ready" if cache_on else "disabled",
                problem=None if cache_on else "the transformation cache is off",
                reason=None if cache_on else "repeat content is re-optimized every time",
                fix=None if cache_on else "set ZIBBO_CACHE_ENABLED=true",
            )
        )
        transformers = status.get("transformers", [])
        checks.append(
            _check(
                "transformers",
                "ok" if transformers else "warn",
                ", ".join(transformers) if transformers else "none registered",
                problem=None if transformers else "no transformers are registered",
                reason=None if transformers else "nothing would be optimized",
                fix=None if transformers else "check the gateway build",
            )
        )
        checks.append(_check("port", "ok", f"gateway on {client.base_url}"))

    if claude is not None:
        route = claude.get("anthropic_route")
        checks.append(
            _check(
                "provider",
                "ok" if route else "warn",
                f"anthropic route at {route}" if route else "anthropic route not mounted",
                problem=None if route else "the Anthropic route is not available",
                reason=None if route else "Claude Code traffic has nowhere to go",
                fix=None if route else "enable the anthropic provider in configuration",
            )
        )
        gv, pv = claude["gateway_version"], _PLUGIN_VERSION
        match = gv == pv
        checks.append(
            _check(
                "version",
                "ok" if match else "warn",
                f"gateway {gv}, CLI {pv}, API v{claude['internal_api_version']}",
                problem=None if match else "gateway and CLI versions differ",
                reason=None if match else "some features may not line up",
                fix=None if match else "reinstall so both match (pipx upgrade zibbo)",
            )
        )

    # Client-side, authoritative — always run.
    auth = detect_auth(os.environ, read_claude_settings())
    checks.append(
        _check(
            "authentication",
            "ok" if auth.present else "warn",
            auth.label if auth.present else "no Claude credentials detected",
            problem=None if auth.present else "Claude Code is not authenticated here",
            reason=None if auth.present else auth.detail,
            fix=None if auth.present else "log in with  claude  — no API key needed",
        )
    )

    routing = detect_routing(os.environ, client.base_url)
    routing_fix = f"export ANTHROPIC_BASE_URL={routing.expected_base_url} , then relaunch"
    checks.append(
        _check(
            "routing",
            "ok" if routing.routed else "warn",
            "through Zibbo" if routing.routed else "not routed through Zibbo",
            problem=None if routing.routed else "this session's Claude traffic bypasses Zibbo",
            reason=None if routing.routed else routing.reason,
            fix=None if routing.routed else routing_fix,
        )
    )

    base_set = "ANTHROPIC_BASE_URL" in os.environ
    checks.append(
        _check(
            "environment variables",
            "ok" if base_set else "warn",
            "ANTHROPIC_BASE_URL set" if base_set else "ANTHROPIC_BASE_URL not set",
            problem=None if base_set else "the routing variable is missing",
            reason=None if base_set else "Claude Code will talk to Anthropic directly",
            fix=None if base_set else f"export ANTHROPIC_BASE_URL={routing.expected_base_url}",
        )
    )

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    checks.append(
        _check(
            "plugin",
            "ok" if plugin_root else "warn",
            "invoked via the Claude Code plugin" if plugin_root else "run from a plain shell",
            problem=None if plugin_root else "not launched by the Claude Code plugin",
            reason=None if plugin_root else "harmless when running zibbo directly",
            fix=None if plugin_root else "use /zibbo inside Claude Code for the integrated view",
        )
    )

    in_cc = os.environ.get("CLAUDECODE", "").strip().lower() not in ("", "0", "false")
    checks.append(
        _check(
            "claude code",
            "ok" if in_cc else "warn",
            "inside a Claude Code session" if in_cc else "not inside Claude Code",
            problem=None if in_cc else "not running inside Claude Code",
            reason=None if in_cc else "detection is best-effort outside the editor",
            fix=None if in_cc else "run this from Claude Code (or ignore when using the CLI)",
        )
    )
    return checks


def _cmd_doctor(client: Client, _args: argparse.Namespace) -> int:
    checks = _build_doctor_checks(client)
    healthy = all(check["status"] != "fail" for check in checks)
    print(render_doctor({"healthy": healthy, "checks": checks}))
    return 0 if healthy else 1


def _cmd_benchmark(client: Client, args: argparse.Namespace) -> int:
    if args.suite:
        return _run_suite(args)
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


def _run_suite(args: argparse.Namespace) -> int:
    """Delegate to the offline benchmark suite (needs the repo + gateway installed).

    The suite runs the real pipeline in-process and writes reports; it is heavier than
    this stdlib CLI, so it lives in ``benchmarks.coding`` and is invoked as a subprocess
    rather than imported here. Absent (installed without the repo) -> a clear hint.
    """
    import subprocess

    command = [sys.executable, "-m", "benchmarks.coding"]
    if args.provider:
        command += ["--provider", args.provider]
    if args.project:
        command += ["--project", args.project]
    if args.print_report:
        command.append("--print")
    try:
        return subprocess.run(command, check=False).returncode  # noqa: S603 — fixed argv
    except FileNotFoundError:
        print(
            "The benchmark suite runs from the Zibbo repository "
            "(python -m benchmarks.coding). Clone it and run there.",
            file=sys.stderr,
        )
        return 2


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


def _cmd_explain(client: Client, _args: argparse.Namespace) -> int:
    print(render_explain(client.get("/internal/logs?limit=1")))
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
            f"{_CROSS} Zibbo gateway is not installed in this environment.\n"
            f"    Install it with:  {_INSTALL_HINT}\n"
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
    "banner": _cmd_banner,
    "claude": _cmd_claude,
    "stats": _cmd_stats,
    "doctor": _cmd_doctor,
    "benchmark": _cmd_benchmark,
    "enable": _cmd_enable,
    "disable": _cmd_disable,
    "logs": _cmd_logs,
    "explain": _cmd_explain,
    "version": _cmd_version,
    "start": _cmd_start,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zibbo", description="Talk to a running Zibbo gateway.")
    parser.add_argument("--url", help=f"gateway base URL (default: discover, then {DEFAULT_URL})")
    parser.add_argument("--token", help="bearer token for a remote-enabled internal API")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Zibbo activation dashboard")
    sub.add_parser("banner", help="compact activation banner (used at session start)")
    sub.add_parser("claude", help="Claude Code activation status (gateway view)")
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
    bench.add_argument(
        "--suite", action="store_true", help="run the full offline benchmark suite + reports"
    )
    bench.add_argument("--provider", help="suite: provider to price/count against")
    bench.add_argument("--project", help="suite: limit to one project")
    bench.add_argument(
        "--print-report", action="store_true", help="suite: echo the markdown report"
    )

    logs = sub.add_parser("logs", help="recent optimization activity (metadata only)")
    logs.add_argument("--limit", type=int, default=20)

    sub.add_parser("explain", help="explain why the last request's tokens were reduced")

    start = sub.add_parser("start", help="start the gateway if it is not already running")
    start.add_argument("--port", type=int, help="port to start on (sets ZIBBO_PORT)")

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    args = build_parser().parse_args(argv)
    _debug(f"command: {args.command}")
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
