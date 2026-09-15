#!/bin/zsh
set -u

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || exit 1

PORT="${ROAST_COMPANION_PORT:-8765}"
EXISTING_PID="$(/usr/sbin/lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1)"
if [[ -n "$EXISTING_PID" ]]; then
  EXISTING_COMMAND="$(ps -p "$EXISTING_PID" -o command= 2>/dev/null)"
  if [[ "$EXISTING_COMMAND" == *"$SCRIPT_DIR/server.py"* ]]; then
    echo "Closing the existing Peak Roasting app..."
    kill "$EXISTING_PID"
    for _ in {1..30}; do
      kill -0 "$EXISTING_PID" 2>/dev/null || break
      sleep 0.1
    done
  else
    echo "Port $PORT is being used by another program. Nothing was stopped."
    echo "$EXISTING_COMMAND"
    read -k 1 "?Press any key to close."
    exit 1
  fi
fi

echo "Starting Peak Roasting (offline)..."
echo "Artisan must be closed while this app reads the Phidget."
echo
PYTHONPATH="$SCRIPT_DIR:$SCRIPT_DIR/../phidget-offline-diagnostics/vendor" /usr/bin/python3 "$SCRIPT_DIR/server.py"
STATUS=$?
echo
echo "Peak Roasting stopped with status $STATUS."
read -k 1 "?Press any key to close."
exit $STATUS
