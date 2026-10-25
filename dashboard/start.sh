#!/usr/bin/env bash
# Run the Hermes dashboard locally: one server at http://127.0.0.1:8000 serves the app and its API, until Ctrl-C.
#
#     dashboard/start.sh                (from the repo root, or from anywhere by its path)
#     dashboard/start.sh --save-menus   (first saves the competitors' menu pages: online, and it makes ASI calls)
#
# Before starting it does the README's one-time setup, each step only when it is missing:
#   - NESSIE_API_KEY and ASI1_API_KEY in the repo-root .env (gitignored), copied from nessie_lab/.env when they are there
#   - nessie_lab/data/cost_price.db (`cost-price build`, from the sample books and the seed competitors)
#   - the web app's packages, and its build (dashboard/web/dist), rebuilt when the app's sources change
# and says what it can't do for you: the sample books, a Bookkeeper report, saved competitor menus.
# DASHBOARD_PORT changes the port (default 8000).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
LAB=$ROOT/nessie_lab
WEB=$ROOT/dashboard/web
PORT=${DASHBOARD_PORT:-8000}
SAVE_MENUS=false
for arg; do
  case $arg in
    --save-menus) SAVE_MENUS=true ;;
    *) echo "usage: dashboard/start.sh [--save-menus]" >&2; exit 2 ;;
  esac
done
cd "$ROOT"

say() { echo "start.sh: $*"; }
warn() { echo "start.sh: note: $*" >&2; }
fail() { echo "start.sh: $*" >&2; exit 1; }

if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  fail "port $PORT is in use; is the dashboard already running? Open http://127.0.0.1:$PORT"
fi
command -v uv >/dev/null || fail "uv is not installed: https://docs.astral.sh/uv/"
command -v node >/dev/null || fail "Node 20 or newer is not installed"
(( $(node -p 'process.versions.node.split(".")[0]') >= 20 )) || fail "Node 20 or newer is needed, not $(node --version)"

# The keys: the server reads them from its environment or the repo-root .env, never from nessie_lab/.env
declare -A NEEDED_FOR=([NESSIE_API_KEY]="connecting the bank" [ASI1_API_KEY]="the Chat")
for key in NESSIE_API_KEY ASI1_API_KEY; do
  [[ -n ${!key:-} ]] && continue
  grep -qE "^$key=.+" .env 2>/dev/null && continue
  if line=$(grep -E "^$key=.+" "$LAB/.env" 2>/dev/null | tail -n 1) && [[ -n $line ]]; then
    [[ -s .env && -n $(tail -c 1 .env) ]] && echo >> .env
    (umask 077; printf '%s\n' "$line" >> .env)
    say "copied $key from nessie_lab/.env to .env"
  else
    warn "$key is not set, so ${NEEDED_FOR[$key]} won't work: add it to .env"
  fi
done

# The sample restaurant's books: every check reads them
[[ -f $LAB/data/books.db ]] ||
  fail "no nessie_lab/data/books.db: generate the sample books first (python generate.py in nessie_lab/, see its README)"

# The menu, plate costs and competitor list
if [[ ! -f $LAB/data/cost_price.db ]]; then
  say "building nessie_lab/data/cost_price.db (once)"
  (cd "$LAB" && uv run --project cost_price cost-price build >/dev/null)
fi

# Saved competitor menus: offline, a market scan reads only saved copies
if $SAVE_MENUS; then
  say "saving the competitors' menu pages (online; this makes ASI calls)"
  (cd "$LAB" && uv run --project market_scout market-scout run --request-id "menus-$(date +%Y%m%d-%H%M%S)" >/dev/null &&
    uv run --project market_scout market-scout pages | tail -n 1)
elif [[ -z $(ls -A "$LAB/data/market_pages" 2>/dev/null) ]]; then
  warn "no saved competitor menus, so the market comparison says \"insufficient data\" (run once with --save-menus)"
fi

# A Bookkeeper report: the review of findings, and the suggestions after it, need one
if [[ ! -f $LAB/data/orchestrator/bookkeeper_state.json ]] && ! compgen -G "$LAB/data/reports/R-*.json" >/dev/null; then
  warn "no Bookkeeper report yet, so there are no findings to review (nessie_lab/data/backup_reports/README.md says how to restore the saved one)"
fi

# The web app: its packages, then the build the server serves
if command -v pnpm >/dev/null; then PNPM=(pnpm); else PNPM=(npx --yes pnpm@9); fi
if [[ ! -f $WEB/node_modules/.modules.yaml || $WEB/pnpm-lock.yaml -nt $WEB/node_modules/.modules.yaml ]]; then
  say "installing the web app's packages"
  (cd "$WEB" && "${PNPM[@]}" install --frozen-lockfile)
fi
if [[ ! -f $WEB/dist/index.html ]] || [[ -n $(find "$WEB/src" "$WEB/public" "$WEB/index.html" "$WEB/package.json" \
     "$WEB/vite.config.ts" "$WEB"/tsconfig*.json -newer "$WEB/dist/index.html" -print -quit) ]]; then
  say "building the web app"
  (cd "$WEB" && "${PNPM[@]}" run build)
fi

say "starting the Hermes dashboard: open http://127.0.0.1:$PORT (Ctrl-C stops it)"
exec uv run --project dashboard uvicorn main:app --app-dir dashboard/api --host 127.0.0.1 --port "$PORT" --reload --reload-dir dashboard/api --timeout-graceful-shutdown 2
