#!/usr/bin/env sh
# Zibbo — resolve the CLI robustly and pass every argument through. Backs the /zibbo
# slash command so it works whether the gateway is installed as a console script or only
# as an importable module in a virtualenv. Set ZIBBO_DEBUG=1 for resolution logging.

debug() { [ -n "$ZIBBO_DEBUG" ] && printf '[zibbo:debug] %s\n' "$1" >&2; return 0; }

if command -v zibbo >/dev/null 2>&1; then
  debug "resolved CLI: zibbo (console script)"
  exec zibbo "$@"
fi

for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c "import gateway" >/dev/null 2>&1; then
    debug "resolved CLI: $py -m gateway.cli"
    exec "$py" -m gateway.cli "$@"
  fi
done

printf 'Zibbo CLI not found. Install it where Claude Code can see it:\n'
printf '  pipx install git+https://github.com/MohammedZaid-AI/zibbo\n'
printf 'Then reload this session. Details: https://github.com/MohammedZaid-AI/zibbo#install\n'
exit 127
