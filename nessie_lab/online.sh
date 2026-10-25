#!/usr/bin/env bash
# Bring Hermes's agents online on Agentverse: the Bookkeeper, its Verifier, the Market scout and the orchestrator,
# which talk to each other through their Agentverse mailboxes. All four stay online until Ctrl-C.
#
#     ./online.sh          (from nessie_lab/, or from anywhere by its path)
#
# This script is how they go online: it sets <AGENT>_MAILBOX only for the processes it starts and leaves .env alone,
# so a plain `serve` stays local. Seeds (BOOKKEEPER_SEED, VERIFIER_SEED, MARKET_SCOUT_SEED, ORCHESTRATOR_SEED),
# ASI1_API_KEY, BOOKKEEPER_ADDR, SCOUT_ADDR, MARKET_SCOUT_ALLOWED_SENDERS and, the first time a seed is used,
# AGENTVERSE_TOKEN come from nessie_lab/.env. An agent without a mailbox is started once with its inspector on, its mailbox is created through
# POST /connect, and it is restarted without the inspector. The agents share this folder's data/: the Bookkeeper's
# and the scout's replies name a report file, and the orchestrator reads it from here.
set -euo pipefail
set -m                      # each agent in its own process group: Ctrl-C reaches this script, which stops them cleanly

cd "$(dirname "$0")"        # nessie_lab: the agents' data root and .env
uv sync --project bookkeeper --quiet
uv sync --project market_scout --quiet
uv sync --project orchestrator --quiet
BK=bookkeeper/.venv/bin/bookkeeper
ORCH=orchestrator/.venv/bin
export PYTHONUNBUFFERED=1   # print the agents' logs as they happen

NAMES=(verifier bookkeeper market_scout orchestrator)           # start order: each one's peers are up first
declare -A PORT=([verifier]=8003 [bookkeeper]=8001 [market_scout]=8006 [orchestrator]=8005) PID=() ADDRESS=()

listening() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

exited() {  # seconds pids: wait until every pid has exited, at most that long
  local seconds=$1 pid alive i
  shift
  for ((i = 0; i < seconds * 2; i++)); do
    alive=0
    for pid; do kill -0 "$pid" 2>/dev/null && alive=1; done
    ((alive)) || return 0
    sleep 0.5
  done
  return 1
}

stop() {  # names: SIGINT for a clean uagents shutdown; SIGTERM after 10 s; SIGKILL 5 s later, because uvicorn under
          # the agent catches both signals and waits for open requests, and `bookkeeper-setup --agents` holds one
  local name pids=()
  for name; do
    [[ -n ${PID[$name]:-} ]] && pids+=("${PID[$name]}") && unset "PID[$name]"
  done
  ((${#pids[@]})) || return 0
  kill -INT "${pids[@]}" 2>/dev/null || true
  exited 10 "${pids[@]}" || { kill -TERM "${pids[@]}" 2>/dev/null || true; exited 5 "${pids[@]}"; } ||
    kill -KILL "${pids[@]}" 2>/dev/null || true
  wait "${pids[@]}" 2>/dev/null || true
}
trap 'stop "${!PID[@]}"' EXIT
trap 'exit 130' INT TERM

start() {  # name inspector: one agent in mailbox mode, its output prefixed with its name; returns once it listens
  local name=$1 prefix=${1^^}
  (
    export "${prefix}_MAILBOX=true" "${prefix}_INSPECTOR=$2"
    case $name in
      verifier) exec "$BK" serve-verifier --port "${PORT[$name]}" ;;
      bookkeeper) exec "$BK" serve --port "${PORT[$name]}" ;;
      market_scout) exec market_scout/.venv/bin/market-scout serve --port "${PORT[$name]}" ;;
      orchestrator) exec "$ORCH/orchestrator" serve ;;
    esac
  ) > >(trap '' INT; sed -u "s/^/[$name] /") 2>&1 &
  PID[$name]=$!
  for _ in {1..120}; do
    listening "${PORT[$name]}" && return
    kill -0 "${PID[$name]}" 2>/dev/null || break
    sleep 0.5
  done
  echo "online.sh: the $name did not start (see its output above)" >&2
  exit 1
}

connect() {  # name: create the mailbox of the agent running with its inspector on (AGENTVERSE_TOKEN)
  case $1 in
    orchestrator) "$ORCH/python" -m orchestrator.register --port "${PORT[$1]}" ;;
    *) "$BK" mailbox-connect --port "${PORT[$1]}" ;;
  esac
}

for name in "${NAMES[@]}"; do
  if listening "${PORT[$name]}"; then
    echo "online.sh: port ${PORT[$name]} is in use; is the $name already running?" >&2
    exit 1
  fi
done

status=$("$BK" mailbox-status "${NAMES[@]^^}")
while read -r name address state; do
  ADDRESS[$name]=$address
  if [[ $state == missing ]]; then
    echo "online.sh: creating the Agentverse mailbox of the $name ($address)"
    start "$name" true
    connect "$name"
    stop "$name"
  fi
done <<<"$status"

for name in "${NAMES[@]}"; do start "$name" false; done
echo
echo "Online on Agentverse until Ctrl-C:"
for name in "${NAMES[@]}"; do printf '  %-13s %s\n' "$name" "${ADDRESS[$name]}"; done
echo

wait -n "${PID[@]}" || true
echo "online.sh: an agent stopped; stopping the others" >&2
exit 1
