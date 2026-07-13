#!/usr/bin/env sh
# Zibbo — Claude Code SessionStart hook.
#
# Starts the local gateway (a fast no-op if it is already running) and prints the Zibbo
# activation banner: gateway, authentication, routing, optimization, and cache — plus the
# exact command to enable routing when it is not yet on. It never blocks or fails a
# session: it always exits 0. Set ZIBBO_DEBUG=1 for verbose lifecycle logging.

debug() { [ -n "$ZIBBO_DEBUG" ] && printf '[zibbo:debug] %s\n' "$1" >&2; return 0; }

debug "SessionStart hook firing (source=${CLAUDE_PLUGIN_ROOT:-unknown})"

# Resolve the CLI: prefer the installed console script, then the module under any Python
# that can import the package. The module path works even when `zibbo` is not on PATH,
# which is the common case when the gateway lives in a virtualenv.
ZIBBO=""
if command -v zibbo >/dev/null 2>&1; then
  ZIBBO="zibbo"
  debug "resolved CLI: zibbo (console script)"
else
  for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1 && "$py" -c "import gateway" >/dev/null 2>&1; then
      ZIBBO="$py -m gateway.cli"
      debug "resolved CLI: $ZIBBO"
      break
    fi
  done
fi

if [ -z "$ZIBBO" ]; then
  printf 'Zibbo: not found on PATH, so nothing was started.\n'
  printf '  Install it where Claude Code can see it:\n'
  printf '    pipx install git+https://github.com/MohammedZaid-AI/zibbo\n'
  printf '  Details: https://github.com/MohammedZaid-AI/zibbo#install\n'
  exit 0
fi

debug "starting gateway (no-op if already running)"
# shellcheck disable=SC2086 -- $ZIBBO may be "python -m gateway.cli"; word-splitting is intended.
$ZIBBO start >/dev/null 2>&1 || printf 'Zibbo: gateway did not start — run  /zibbo doctor  for details.\n'

# Render the activation banner: gateway, authentication, routing, optimization, cache —
# and, when routing is not yet on, the one exact command to enable it. The CLI computes
# all of this from Claude Code's own environment, which this hook shares. If the gateway
# is unreachable the banner says so and how to start it.
debug "rendering activation banner"
# shellcheck disable=SC2086 -- intentional word-splitting on $ZIBBO.
$ZIBBO banner
exit 0
