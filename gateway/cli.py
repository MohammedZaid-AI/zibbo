"""The ``zibbo`` command-line client.

A thin, dependency-free terminal client for a running gateway's internal API. It is the
bridge between the gateway (the engine) and the editor plugins (the UX): the Claude Code
and Codex plugins shell out to ``zibbo`` rather than re-implementing any of this, so all
the intelligence stays in one place and users get a usable CLI for free.

Stdlib only — ``urllib``, ``argparse``, ``json`` — so it imports even where the gateway's
own dependencies are not installed, and starts instantly. It talks to the gateway over
the same public /internal/* HTTP API the plugins use; it never reaches into gateway
internals.

    zibbo [status] | banner | claude | stats | doctor | explain | benchmark | logs
          | enable | disable | version | start

A bare ``zibbo`` shows the dashboard, so the plugin's ``!`zibbo $ARGUMENTS`` works with no
argument. ``zibbo banner --start`` (the SessionStart hook) starts the gateway, then renders.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING, Any

import gateway
from gateway import endpoint, lifecycle, plugin_dev
from gateway.claude_env import detect_auth, detect_routing, read_claude_settings

if TYPE_CHECKING:
    from pathlib import Path

_PLUGIN_VERSION = gateway.__version__

# The gateway's location and identity resolve through gateway.endpoint — the single source
# of truth shared with the server and Settings — so the CLI never re-derives a port or a
# default that could drift from what the gateway actually binds.
DEFAULT_URL = endpoint.DEFAULT_BASE_URL
_DISCOVERY_PORTS = (endpoint.DEFAULT_PORT, 8080, 8123)
_ENV_URL = endpoint.ENV_GATEWAY_URL
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
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            _debug(f"{method} {url} -> HTTP {exc.code}: {detail}")
            raise GatewayError(f"gateway returned {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            _debug(f"{method} {url} -> unreachable: {exc}")
            raise GatewayError(f"no gateway at {self._base} ({exc})") from exc
        try:
            # A server answered, but it may not be Zibbo — a stray dev server or an unrelated
            # service on this port answers with HTML/plain text. Treat unparseable output as
            # "not a gateway" rather than letting a JSONDecodeError crash every command.
            return json.loads(raw or b"null")
        except ValueError as exc:
            _debug(f"{method} {url} -> non-JSON response: {exc}")
            raise GatewayError(f"{self._base} answered but is not a Zibbo gateway") from exc

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, body)

    def reachable(self) -> bool:
        """True only when *Zibbo* answers, not merely when the port is occupied.

        Identity is confirmed against the gateway's own protocol contract
        (:func:`gateway.endpoint.is_gateway_identity`): the ``service`` field on
        ``/internal/version``, with a back-compat fallback to ``internal_api_version``. This
        stops an unrelated HTTP service on the gateway's port from being mistaken for Zibbo
        (a false positive that makes ``start`` claim "already running" and ``doctor`` report
        a healthy gateway whose real routes then 404).
        """
        try:
            payload = self.get("/internal/version")
        except GatewayError:
            return False
        return endpoint.is_gateway_identity(payload)


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


def default_gateway_url() -> str:
    """The gateway's base URL, resolved through :mod:`gateway.endpoint` — the single source
    of truth for its port (env > ``.env`` > default). ``start`` resolves its pre-flight port
    check, its launch, and its readiness poll from this one place, so they can never target
    different ports; discovery and the spawned gateway resolve it the same way.
    """
    return endpoint.default_base_url()


def discover(explicit: str | None, token: str | None) -> Client:
    """Find a reachable gateway: an explicit URL, then $ZIBBO_GATEWAY_URL, then a configured
    port (env or ``.env``), then probe the common ports.

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
    resolved = endpoint.resolve_port()
    if resolved.configured:
        # A configured port (env var or .env) pins where the gateway is; honour it over
        # probing the usual ports, and name the source so a mismatch is diagnosable.
        target = default_gateway_url()
        _debug(f"discovery: honouring port from {resolved.source} -> {target}")
        return Client(target, token=token)
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
        "    zibbo connect",
        "",
        "  Then restart Claude Code.",
    ]
    return out


def _kv_flag(ok: bool, key: str, value: str) -> str:
    return f"  {_TICK if ok else _CROSS} {key:<15}{value}"


def _sub_flag(ok: bool, key: str, value: str) -> str:
    return f"      {_TICK if ok else _CROSS} {key:<12}{value}"


def _effective_routed(routing: dict[str, Any]) -> bool:
    """Reality wins: observed traffic settles it; otherwise fall back to configuration."""
    if routing.get("observed"):
        return True
    return bool(routing.get("configured"))


def _auth_effective(auth: dict[str, Any]) -> dict[str, Any]:
    """Prefer authentication actually observed on the wire over environment intent."""
    observed: dict[str, Any] | None = auth.get("observed")
    if observed and observed.get("present"):
        return observed
    configured: dict[str, Any] = auth["configured"]
    return configured


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

    routing = banner["routing"]
    cache = banner["cache"]
    routed = _effective_routed(routing)
    eff_auth = _auth_effective(banner["auth"])
    optimizing = banner["optimization_enabled"]
    lamp = _GREEN if routed else _YELLOW
    headline = "Zibbo Active" if routed else "Zibbo - one step left"
    if routing.get("observed"):
        routing_value = "Through Zibbo (traffic seen)"
    elif routing.get("configured"):
        routing_value = "Configured (no traffic yet)"
    else:
        routing_value = "Not routed"
    lines = [
        f"{lamp} {headline}".strip(),
        "",
        _kv_flag(True, "Gateway", f"Running ({gateway['version']})"),
        _kv_flag(eff_auth["present"], "Authentication", eff_auth["label"]),
        _kv_flag(routed, "Routing", routing_value),
        _kv_flag(optimizing, "Optimization", "Enabled" if optimizing else "Disabled"),
        _kv_flag(
            cache["enabled"], "Cache", f"Ready ({cache['backend']})" if cache["enabled"] else "Off"
        ),
    ]
    if routed:
        lines += ["", "  Try  /zibbo:zibbo  (dashboard)  ·  /zibbo:stats  ·  /zibbo:doctor"]
    else:
        lines += _routing_help(eff_auth, routing)
    return "\n".join(lines)


# The only transformer that can be registered or not at runtime today. Listed so its
# *absence* from the registry is shown explicitly, rather than silently missing.
_OPTIONAL_TRANSFORMERS = ("prompt",)


def _transformer_lines(active: list[str]) -> list[str]:
    """A ✓ for every registered transformer, and a ✗ for a known-optional one that is off.

    ``active`` is the gateway's live registry (``/internal/status`` -> ``transformers``),
    so this reflects what would actually run, not a hardcoded list.
    """
    lines = ["", "  Transformers"]
    for name in active:
        lines.append(f"      {_TICK} {name}")
    for name in _OPTIONAL_TRANSFORMERS:
        if name not in active:
            lines.append(f"      {_CROSS} {name} (disabled)")
    return lines


def render_dashboard(dash: dict[str, Any]) -> str:
    """The ``/zibbo`` / ``zibbo status`` dashboard: configured intent vs observed reality."""
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
        ("Requests today", f"{stats['requests']:,}"),
        ("Average reduction", f"{stats['token_reduction_pct']}%"),
        ("Cache hit rate", f"{round(stats['cache_hit_rate'] * 100, 1)}%"),
        ("Estimated savings", savings),
        ("Optimization", "Enabled" if dash["optimization_enabled"] else "Disabled"),
        ("Cache", f"Ready ({cache['backend']})" if cache["enabled"] else "Off"),
    ]
    lines = ["Zibbo", ""]
    lines += [f"  {label:<20}{value}" for label, value in rows]
    lines += _transformer_lines(dash.get("transformers", []))

    # Routing: configuration (this shell's intent) beside observation (the gateway's
    # reality). Showing both is what exposes an environment mismatch at a glance.
    configured = bool(routing.get("configured"))
    observed = bool(routing.get("observed"))
    lines += ["", "  Routing"]
    lines.append(_sub_flag(configured, "Configured", "Yes" if configured else "No"))
    lines.append(_sub_flag(observed, "Observed", "Active" if observed else "none yet"))

    # Endpoint: the concrete URL Claude Code is (or will be) pointed at, and whether that is
    # the Zibbo gateway or the provider directly. Reflects the persisted settings file.
    endpoint = dash.get("endpoint", "https://api.anthropic.com")
    cc_configured = bool(dash.get("cc_configured", False))
    lines += ["", "  Endpoint"]
    cc_label = "Configured" if cc_configured else "Default"
    lines.append(_sub_flag(cc_configured, "Claude Code", cc_label))
    lines.append(f"      {_DOT} {'Current':<12}{endpoint}")

    lines += ["", "  Authentication"]
    cfg = auth["configured"]
    lines.append(
        _sub_flag(
            cfg["present"],
            "Configured",
            cfg["label"] if cfg["present"] else "not detected in this shell",
        )
    )
    obs_auth = auth.get("observed")
    if obs_auth and obs_auth.get("present"):
        lines.append(_sub_flag(True, "Observed", obs_auth["label"]))
    else:
        lines.append(_sub_flag(False, "Observed", "none yet"))

    routed = _effective_routed(routing)
    healthy = routed and dash["optimization_enabled"]
    lines += ["", f"  {'Status':<20}{'Healthy' if healthy else 'Needs attention'}"]

    if observed and not configured:
        # The exact inconsistency the user hit: traffic is flowing, but the env var is not
        # visible in this shell. That is fine — expose it, do not nag.
        lines += [
            "",
            "  Traffic is flowing through Zibbo, so routing works. ANTHROPIC_BASE_URL is",
            "  not visible in this shell — expected when Claude Code sets it via",
            "  settings.json or a different shell. No action needed.",
        ]
    elif not routed:
        lines += _routing_help(_auth_effective(auth), routing)
    return "\n".join(lines)


def render_claude(data: dict[str, Any]) -> str:
    """Render the raw ``/internal/claude`` view (``zibbo claude``)."""
    configured = data["authentication"]
    observed = data["observed_authentication"]
    route = data["anthropic_route"] or "not mounted"
    seen = data.get("anthropic_requests_observed", 0)
    return "\n".join(
        [
            "Zibbo - Claude Code activation (gateway view)",
            "",
            _flag(data["gateway_running"], f"Gateway running ({data['gateway_version']})"),
            _flag(data["optimization_enabled"], "Optimization enabled"),
            _flag(data["cache_enabled"], f"Cache ({data['cache_backend']})"),
            f"  {_DOT} Anthropic route: {route}",
            _flag(data["routing_observed"], f"Routing observed ({seen} Anthropic requests)"),
            _flag(observed["present"], f"Authentication observed: {observed['label']}"),
            f"  {_DOT} Authentication (gateway env): {configured['label']}",
            "",
            "  Observed values come from actual forwarded traffic (reality). Configured",
            "  values come from environment inspection (intent). The CLI's dashboard reads",
            "  configuration from Claude Code's own environment.",
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
    "removed_duplicate_list_items": "Removed duplicate instructions",
    "removed_duplicate_blocks": "Removed duplicate instruction block",
    "removed_duplicate_sentences": "Removed repeated sentence",
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


def _claude_or_none(client: Client) -> dict[str, Any] | None:
    """Fetch /internal/claude (observed routing + auth), or None when unreachable."""
    try:
        data: dict[str, Any] = client.get("/internal/claude")
    except GatewayError:
        return None
    return data


def _activation_view(client: Client, claude: dict[str, Any] | None) -> dict[str, Any]:
    """Combine configured intent (this shell's env) with observed reality (the gateway).

    Environment variables describe intent; forwarded traffic describes reality. We surface
    both so the two can be compared — and reality is what the effective state derives from.
    """
    auth_cfg = _auth_view()
    routing_cfg = _routing_view(client.base_url)
    observed_auth = None
    if claude is not None:
        oa = claude.get("observed_authentication") or {}
        observed_auth = {"present": bool(oa.get("present")), "label": oa.get("label", "")}
    return {
        "routing": {
            "configured": routing_cfg["routed"],
            "observed": bool(claude["routing_observed"]) if claude is not None else None,
            "expected_base_url": routing_cfg["expected_base_url"],
            "reason": routing_cfg["reason"],
        },
        "auth": {
            "configured": {"present": auth_cfg["present"], "label": auth_cfg["label"]},
            "observed": observed_auth,
        },
    }


def _cmd_status(client: Client, _args: argparse.Namespace) -> int:
    status = _status_or_none(client)
    view = _activation_view(client, _claude_or_none(client))
    if status is None:
        print(render_banner({"gateway": None, **view}))
        return 1
    stats_today: dict[str, Any] = client.get("/internal/stats")["today"]
    providers = status.get("providers", [])
    routed = _effective_routed(view["routing"])
    provider = "Anthropic" if routed else (", ".join(p["name"] for p in providers) or "none")
    # The persisted routing intent (what Claude Code will use next launch) is the settings
    # file, not this shell's env — so status reflects reality across restarts.
    persisted = lifecycle.effective_persisted_base_url()
    gateway_route = lifecycle.desired_base_url(client.base_url)
    env_base = os.environ.get("ANTHROPIC_BASE_URL")
    current_endpoint = persisted or env_base or lifecycle.DIRECT_ENDPOINT
    cc_configured = current_endpoint.rstrip("/") == gateway_route.rstrip("/")
    dash = {
        "gateway": {"version": status["version"], "environment": status["environment"]},
        "provider": provider,
        "auth": view["auth"],
        "routing": view["routing"],
        "stats": stats_today,
        "optimization_enabled": status["optimization_enabled"],
        "cache": {"enabled": status["cache_enabled"], "backend": status["cache_backend"]},
        "transformers": status.get("transformers", []),
        "endpoint": current_endpoint,
        "cc_configured": cc_configured,
    }
    print(render_dashboard(dash))
    return 0


def _cmd_banner(client: Client, args: argparse.Namespace) -> int:
    # The SessionStart hook runs `zibbo banner --start`: bring the gateway up (a no-op if
    # it is already running), then render. One executable, no shell chaining.
    if getattr(args, "start", False):
        _start_gateway(client, announce=False)
    status = _status_or_none(client)
    view = _activation_view(client, _claude_or_none(client))
    if status is None:
        print(render_banner({"gateway": None, **view}))
        return 1
    print(
        render_banner(
            {
                "gateway": {"running": True, "version": status["version"]},
                "auth": view["auth"],
                "routing": view["routing"],
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
    if reachable:
        checks.append(_check("connectivity", "ok", f"gateway reachable at {client.base_url}"))
    elif _port_is_occupied(client.base_url):
        # Something answers on the port, but it is not Zibbo — the case that used to read as a
        # bare "no gateway". Name it so the fix (free the port or move Zibbo) is obvious.
        checks.append(
            _check(
                "connectivity",
                "fail",
                f"a non-Zibbo service is listening on {client.base_url}",
                problem="the gateway URL is occupied by another process",
                reason="something answers on that port, but it is not the Zibbo gateway",
                fix="stop that process, or point Zibbo elsewhere: ZIBBO_PORT=8123 zibbo start",
            )
        )
    else:
        checks.append(
            _check(
                "connectivity",
                "fail",
                f"no gateway at {client.base_url}",
                problem="the CLI cannot reach the gateway",
                reason="nothing is listening on the gateway URL",
                fix="run  zibbo start  (or set ZIBBO_GATEWAY_URL)",
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
        prompt_on = status.get("prompt_optimization_enabled", False)
        checks.append(
            _check(
                "prompt optimizer",
                "ok" if prompt_on else "warn",
                "enabled" if prompt_on else "disabled",
                problem=None if prompt_on else "deterministic prompt de-duplication is off",
                reason=None if prompt_on else "ZIBBO_PROMPT_OPTIMIZATION=false (the default)",
                fix=None
                if prompt_on
                else "set ZIBBO_PROMPT_OPTIMIZATION=true (persists across restarts), "
                "or run  zibbo enable prompt  (this session only)",
            )
        )

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
    routing_fix = "run  zibbo connect  (or  zibbo doctor --fix ), then restart Claude Code"
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
    persisted = lifecycle.effective_persisted_base_url()
    checks.append(
        _check(
            "environment variables",
            "ok" if base_set or persisted else "warn",
            "ANTHROPIC_BASE_URL set"
            if base_set
            else ("configured in settings.json (restart to apply)" if persisted else "not set"),
            problem=None if base_set or persisted else "the routing variable is not configured",
            reason=None if base_set or persisted else "Claude Code will talk to Anthropic directly",
            fix=None if base_set or persisted else "run  zibbo connect",
        )
    )

    resolved = endpoint.resolve_port()
    url_env = os.environ.get(_ENV_URL)
    source = f"${_ENV_URL}" if url_env else f"port from {resolved.source}"
    checks.append(
        _check(
            "configuration",
            "ok",
            f"gateway URL {client.base_url} ({source})",
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


def _cmd_doctor(client: Client, args: argparse.Namespace) -> int:
    checks = _build_doctor_checks(client)
    if getattr(args, "fix", False):
        routing_ok = any(c["name"] == "routing" and c["status"] == "ok" for c in checks)
        if routing_ok:
            print(f"{_TICK} Routing is already configured — nothing to fix.\n")
        else:
            print("Routing Claude Code through Zibbo…\n")
            return _connect(client, args)  # writes settings, prints result + restart hint
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


def _toggle_path(action: str, feature: str | None) -> str:
    path = f"/internal/{action}"
    if feature:
        path += f"?feature={feature}"
    return path


def _render_toggle(data: dict[str, Any], feature: str | None) -> str:
    if feature == "prompt":
        state = "enabled" if data.get("prompt_optimization_enabled") else "disabled"
        return f"Prompt optimization {state}."
    return f"Optimization {'enabled' if data['optimization_enabled'] else 'disabled'}."


def _cmd_enable(client: Client, args: argparse.Namespace) -> int:
    feature = _normalize_feature(getattr(args, "feature", None))
    data = client.post(_toggle_path("enable", feature))
    print(_render_toggle(data, feature))
    return 0


def _cmd_disable(client: Client, args: argparse.Namespace) -> int:
    feature = _normalize_feature(getattr(args, "feature", None))
    data = client.post(_toggle_path("disable", feature))
    print(_render_toggle(data, feature))
    return 0


def _normalize_feature(feature: str | None) -> str | None:
    """Only ``prompt`` is a per-feature target today; anything else is the global switch."""
    if feature and feature.strip().lower() == "prompt":
        return "prompt"
    return None


def _cmd_logs(client: Client, args: argparse.Namespace) -> int:
    print(render_logs(client.get(f"/internal/logs?limit={args.limit}")))
    return 0


def _cmd_explain(client: Client, _args: argparse.Namespace) -> int:
    print(render_explain(client.get("/internal/logs?limit=1")))
    return 0


def _cmd_plugin(_client: Client, args: argparse.Namespace) -> int:
    """Developer tooling for the Claude Code plugin (filesystem only; no gateway needed)."""
    try:
        repo = plugin_dev.repo_plugin_dir(args.plugin_dir)
    except FileNotFoundError as exc:
        print(f"{_CROSS} {exc}", file=sys.stderr)
        return 2
    action = getattr(args, "action", "status") or "status"
    if action == "dev":
        return _plugin_dev(repo)
    if action == "verify":
        return _plugin_verify(repo)
    if action == "sync":
        return _plugin_sync(repo)
    return _plugin_status(repo)


def _plugin_verify(repo: Path) -> int:
    problems = plugin_dev.verify_plugin_dir(repo)
    if problems:
        print(f"{_CROSS} plugin files are NOT clean:")
        for problem in problems:
            print(f"    - {problem}")
        return 1
    print(f"{_TICK} plugin files are clean: exec-form hook, `!`zibbo $ARGUMENTS``, no expansion.")
    return 0


def _plugin_status(repo: Path) -> int:
    repo_version = plugin_dev.read_plugin_version(repo)
    print("Zibbo plugin")
    print(f"  {_TICK} Repository version: {repo_version}")
    print(f"       {repo}")
    installed = plugin_dev.installed_cache_dirs()
    if not installed:
        print(f"  {_WARN} No installed copy under {plugin_dev.claude_plugins_home()}/cache")
        print("       Install:  /plugin marketplace add MohammedZaid-AI/zibbo")
        print("                 /plugin install zibbo@zibbo")
        return 0
    content_stale = False
    version_mismatch = False
    for directory in installed:
        version = plugin_dev.read_plugin_version(directory) or directory.name
        problems = plugin_dev.verify_plugin_dir(directory)
        matches = version == repo_version and not problems
        print(f"  {_TICK if matches else _WARN} Installed version: {version}")
        print(f"       {directory}")
        for problem in problems:
            content_stale = True
            print(f"       stale content: {problem}")
        if version != repo_version and not problems:
            version_mismatch = True
            print(f"       content matches repo; version label is {version} (repo {repo_version})")
    if content_stale:
        print()
        print("  The installed copy has stale content (the executed files are out of date).")
        print("    zibbo plugin sync      # refresh the installed cache now (dev)")
        print("    zibbo plugin dev       # or load the repo directly with --plugin-dir")
        return 1
    if version_mismatch:
        print()
        print("  Content is current. For a clean version bump, push and run /plugin update")
        print("  (see 'Developing the Claude Code plugin' in the plugin README).")
        return 0
    print(f"\n  {_TICK} Installed copy matches the repository.")
    return 0


def _plugin_dev(repo: Path) -> int:
    print("Zibbo plugin - local development")
    print()
    print("  Load the repository copy directly. It overrides the installed marketplace")
    print("  plugin for the session - no cache, no version bump, instant:")
    print()
    print(f'    claude --plugin-dir "{repo}"')
    print()
    print("  After editing hooks or commands, run  /reload-plugins  in Claude Code.")
    print("  This is the supported iteration workflow; `zibbo plugin sync` is for")
    print("  refreshing an already-installed marketplace copy.")
    return 0


def _plugin_sync(repo: Path) -> int:
    problems = plugin_dev.verify_plugin_dir(repo)
    if problems:
        print(f"{_CROSS} refusing to sync - the repository plugin itself is not clean:")
        for problem in problems:
            print(f"    - {problem}")
        return 1
    targets = plugin_dev.installed_cache_dirs()
    clone = plugin_dev.marketplace_clone_dir()
    if clone is not None:
        targets.append(clone)
    if not targets:
        print(f"{_WARN} nothing installed to sync under {plugin_dev.claude_plugins_home()}")
        print("      Use  zibbo plugin dev  for the --plugin-dir workflow, or install first.")
        return 1
    repo_version = plugin_dev.read_plugin_version(repo)
    print("Zibbo plugin sync")
    print(f"  {_TICK} Repository version: {repo_version}")
    for target in targets:
        written = plugin_dev.sync_plugin(repo, target)
        remaining = plugin_dev.verify_plugin_dir(target)
        if remaining:
            print(f"  {_CROSS} {target}")
            for problem in remaining:
                print(f"       still stale: {problem}")
            return 1
        print(f"  {_TICK} Refreshed {', '.join(written)}")
        print(f"       {target}")
    print()
    print(f"  {_TICK} Cache refreshed. Restart Claude Code (or run /reload-plugins).")
    return 0


def _cmd_version(client: Client, _args: argparse.Namespace) -> int:
    data = client.get("/internal/version")
    api = data["internal_api_version"]
    print(f"{data['app_name']} {data['gateway_version']} (internal API v{api})")
    return 0


def _port_is_occupied(base_url: str) -> bool:
    """True if something is already listening on the gateway's host:port.

    Callers check this only after ``reachable()`` has come back False, so an open port
    means *another* process owns it. Binding a second server on top (Windows allows it via
    ``SO_REUSEADDR``) would make the readiness probe hit the wrong server and never see
    Zibbo — the exact way ``start`` used to hang. Refusing up front surfaces the real cause.
    """
    parts = urllib.parse.urlsplit(base_url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _report_startup_failure(log_path: Path, *, exit_code: int | None) -> None:
    """Surface *why* startup failed — the child's own output — never just a bare timeout."""
    if exit_code is not None:
        print(
            f"{_CROSS} gateway process exited during startup (exit code {exit_code})",
            file=sys.stderr,
        )
    else:
        print(f"{_CROSS} gateway did not become ready in time", file=sys.stderr)
    tail: list[str] = []
    with contextlib.suppress(OSError):
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
    if tail:
        print("    ---- gateway output " + "-" * 39, file=sys.stderr)
        for line in tail:
            print(f"    {line}", file=sys.stderr)
        print("    " + "-" * 59, file=sys.stderr)
    print(f"    full log: {log_path}", file=sys.stderr)


def _start_gateway(client: Client, *, announce: bool) -> int:
    """Start the gateway if it is not already running, then wait for it to answer.

    The port comes from ``client.base_url`` (resolved by ``discover`` through
    ``gateway.endpoint``). The child inherits this process's environment and working
    directory, so its ``Settings`` resolve the identical port (env > ``.env`` > default) —
    pre-flight check, launch, and readiness poll can never target different ports.
    ``announce`` prints success to stdout (the ``start`` command); the banner suppresses
    it so the started gateway shows up in the banner itself, not as chatter above it.
    Progress and errors always go to stderr. Returns 0 running, 1 not ready, 2 not installed.
    """
    if client.reachable():
        if announce:
            print(f"{_TICK} Zibbo already running at {client.base_url}")
        return 0
    if _port_is_occupied(client.base_url):
        print(
            f"{_CROSS} {client.base_url} is already in use by another process that is not Zibbo.\n"
            f"    Stop that process, or start Zibbo on another port:  ZIBBO_PORT=8123 zibbo start",
            file=sys.stderr,
        )
        return 1
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
    # This is a detached background daemon, so force uvicorn's autoreloader off: its reload
    # supervisor's worker subprocess is orphaned/killed under DETACHED_PROCESS and the server
    # would die right after startup. See gateway.__main__._reload_enabled.
    env[endpoint.ENV_RELOAD] = "false"
    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    else:
        start_new_session = True

    # Capture the child's stdout+stderr to a file (not DEVNULL) so a startup crash is
    # visible, not swallowed. A file, not a PIPE: the gateway is detached and outlives this
    # CLI, so a pipe would fill and block once we stop reading — a file the child keeps.
    log_path = lifecycle.startup_log_path()
    log_file: IO[str] | None = None
    with contextlib.suppress(OSError):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("w", encoding="utf-8")
    stdout = log_file if log_file is not None else subprocess.DEVNULL
    stderr = subprocess.STDOUT if log_file is not None else subprocess.DEVNULL

    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "gateway"],
            env=env,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        print(f"Starting Zibbo gateway{_ELLIPSIS}", file=sys.stderr)
        for _ in range(50):  # up to ~10s
            if proc.poll() is not None:  # the gateway process died during startup
                _report_startup_failure(log_path, exit_code=proc.returncode)
                return 1
            time.sleep(0.2)
            if client.reachable():
                # Record the PID we own so `zibbo stop` can terminate this exact process — and
                # only this one. A gateway started another way leaves no PID file and is untouched.
                lifecycle.write_pidfile(proc.pid, client.base_url)
                if announce:
                    print(f"{_TICK} Zibbo running at {client.base_url}")
                return 0
    finally:
        # Close our handle; the detached child keeps its own inherited copy and its log.
        if log_file is not None:
            log_file.close()
    _report_startup_failure(log_path, exit_code=None)
    return 1


def _connect(client: Client, args: argparse.Namespace) -> int:
    """Bring the gateway up *and* route Claude Code through it, transactionally.

    Config is written only after the gateway answers its health check, so a failed startup
    never leaves Claude Code pointed at a dead endpoint. If the config write itself fails, a
    gateway we started this call is stopped again — everything succeeds or nothing persists.

    Routing is written to the global user settings by default, which the VS Code extension
    and the CLI both read regardless of which folder is open; ``--project`` scopes it to the
    current workspace instead.
    """
    scope: lifecycle.Scope = "project" if getattr(args, "project", False) else "user"
    was_running = client.reachable()
    rc = _start_gateway(client, announce=False)
    if rc != 0:
        return rc  # startup failed/timed out — settings untouched, nothing to roll back
    started_now = not was_running

    try:
        result = lifecycle.configure_routing(
            lifecycle.desired_base_url(client.base_url), scope=scope
        )
    except lifecycle.SettingsError as exc:
        if started_now:
            lifecycle.stop_gateway(client.reachable)  # roll back the process we just started
        print(f"{_CROSS} could not configure Claude Code: {exc}", file=sys.stderr)
        print("    Gateway not left routed. Fix the settings file and retry.", file=sys.stderr)
        return 1

    print(f"{_TICK} Gateway running ({client.base_url})")
    if result.already_routed:
        print(f"{_TICK} Claude Code already routed through Zibbo")
    else:
        print(f"{_TICK} Claude Code configured  ({result.settings_path})")
    print()
    print("  Restart Claude Code for routing to take effect.")
    return 0


def _cmd_start(client: Client, args: argparse.Namespace) -> int:
    """Start the gateway and route Claude Code through it (alias-friendly to ``connect``)."""
    return _connect(client, args)


def _cmd_connect(client: Client, args: argparse.Namespace) -> int:
    """One-command onboarding: start the gateway and route Claude Code through it."""
    return _connect(client, args)


def _cmd_disconnect(client: Client, args: argparse.Namespace) -> int:
    """Stop routing Claude Code through Zibbo, leaving the gateway running."""
    scope: lifecycle.Scope = "project" if getattr(args, "project", False) else "user"
    try:
        restore = lifecycle.restore_routing(scope=scope)
    except lifecycle.SettingsError as exc:
        print(f"{_CROSS} could not restore Claude Code settings: {exc}", file=sys.stderr)
        return 1
    if restore.restored_to:
        print(f"{_TICK} Claude Code restored  (ANTHROPIC_BASE_URL -> {restore.restored_to})")
    elif restore.changed:
        print(f"{_TICK} Routing removed  (Claude Code back on the default endpoint)")
    else:
        print(f"{_TICK} Claude Code was not routed through Zibbo")
    print()
    print("  The gateway is still running. Restart Claude Code to return to the normal endpoint.")
    return 0


def _cmd_stop(client: Client, _args: argparse.Namespace) -> int:
    """Stop the gateway we started and restore Claude Code's previous endpoint.

    Config is always restored, even if the process could not be stopped, so Claude Code is
    never left pointing at a gateway that is going away.
    """
    stop = lifecycle.stop_gateway(client.reachable)

    try:
        restore = lifecycle.restore_routing()
    except lifecycle.SettingsError as exc:
        print(f"{_CROSS} could not restore Claude Code settings: {exc}", file=sys.stderr)
        return 1

    if stop.outcome == "stopped":
        print(f"{_TICK} Gateway stopped")
    elif stop.outcome == "not_running":
        print(f"{_TICK} Gateway already stopped")
    elif stop.outcome == "not_owned":
        print(f"{_WARN} Gateway is running but was not started by  zibbo start")
        print("       Stop it where you launched it (uvicorn / docker / another shell).")
    else:  # stop_timeout
        print(f"{_WARN} Gateway did not stop in time; check for a lingering process")

    if restore.restored_to:
        print(f"{_TICK} Claude Code restored  (ANTHROPIC_BASE_URL -> {restore.restored_to})")
    elif restore.changed:
        print(f"{_TICK} Claude Code restored  (removed Zibbo routing)")
    else:
        print(f"{_TICK} Claude Code already on the default endpoint")
    print()
    print("  Restart Claude Code to return to the normal endpoint.")
    return 0


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
    "plugin": _cmd_plugin,
    "version": _cmd_version,
    "start": _cmd_start,
    "stop": _cmd_stop,
    "connect": _cmd_connect,
    "disconnect": _cmd_disconnect,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zibbo", description="Talk to a running Zibbo gateway.")
    parser.add_argument("--url", help=f"gateway base URL (default: discover, then {DEFAULT_URL})")
    parser.add_argument("--token", help="bearer token for a remote-enabled internal API")
    # Optional so a bare `zibbo` (and the plugin's `!`zibbo $ARGUMENTS`` with no argument)
    # defaults to the dashboard instead of erroring.
    sub = parser.add_subparsers(dest="command", required=False)

    sub.add_parser("status", help="Zibbo activation dashboard (default)")
    banner = sub.add_parser("banner", help="compact activation banner (used at session start)")
    banner.add_argument(
        "--start", action="store_true", help="start the gateway first if it is not running"
    )
    sub.add_parser("claude", help="Claude Code activation status (gateway view)")
    sub.add_parser("stats", help="optimization statistics")
    doctor = sub.add_parser("doctor", help="run diagnostics")
    doctor.add_argument(
        "--fix", action="store_true", help="route Claude Code through Zibbo if it isn't already"
    )
    enable = sub.add_parser("enable", help="enable transformations (or a feature: prompt)")
    enable.add_argument("feature", nargs="?", help="feature to enable, e.g. prompt")
    disable = sub.add_parser("disable", help="disable transformations (or a feature: prompt)")
    disable.add_argument("feature", nargs="?", help="feature to disable, e.g. prompt")
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

    plugin = sub.add_parser("plugin", help="developer tooling for the Claude Code plugin")
    plugin.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["status", "verify", "sync", "dev"],
        help="status (default): compare installed vs repo; verify: check for shell "
        "expansion; sync: refresh the installed cache; dev: print the --plugin-dir workflow",
    )
    plugin.add_argument("--plugin-dir", help="path to plugins/claude-code (default: this checkout)")

    start = sub.add_parser("start", help="start the gateway and route Claude Code through it")
    start.add_argument("--port", type=int, help="port to start on (sets ZIBBO_PORT)")
    start.add_argument(
        "--project", action="store_true", help="route this workspace only, not globally"
    )

    sub.add_parser("stop", help="stop the gateway and restore Claude Code's endpoint")

    connect = sub.add_parser(
        "connect", help="start the gateway and route Claude Code through it (recommended)"
    )
    connect.add_argument("--port", type=int, help="port to start on (sets ZIBBO_PORT)")
    connect.add_argument(
        "--project", action="store_true", help="route this workspace only, not globally"
    )

    disconnect = sub.add_parser(
        "disconnect", help="stop routing Claude Code through Zibbo (gateway keeps running)"
    )
    disconnect.add_argument(
        "--project", action="store_true", help="undo project-scoped routing instead of global"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    args = build_parser().parse_args(argv)
    command = args.command or "status"  # bare `zibbo` shows the dashboard
    _debug(f"command: {command}")
    # --port is sugar for ZIBBO_PORT: fold it in before discovery so the pre-flight check,
    # launch, and readiness poll all resolve one port from the single source (gateway.endpoint).
    if getattr(args, "port", None):
        os.environ[endpoint.ENV_PORT] = str(args.port)
    token = args.token or os.environ.get(_ENV_TOKEN)
    client = discover(args.url, token)
    handler = _COMMANDS[command]
    try:
        return handler(client, args)
    except GatewayError as exc:
        print(f"{_CROSS} {exc}", file=sys.stderr)
        print("    Is the gateway running?  Try:  zibbo start", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
