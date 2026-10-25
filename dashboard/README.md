# BackHouse dashboard

The owner's view of BackHouse: a React app (`web/`) and the FastAPI server behind it (`api/`). The server runs the
nessie_lab pipeline as a check, serves the forecast and what-ifs, and answers the Chat through ASI:One.

## Run it

You need uv, Node 20 or newer with pnpm, and a `.env` at the repo root with `NESSIE_API_KEY` and `ASI1_API_KEY`. The keys
stay on the server; the browser never sees them. A check reads the sample restaurant's books, so generate them once
first (`python generate.py` in `nessie_lab/`, see its README).

A check ends with the orchestrator's brief, which runs in the orchestrator's own environment (uv sets it up on the first
check). It needs two more things once, from `nessie_lab/`:

    uv run --project cost_price cost-price build                        # the menu, plate costs and competitor list
    uv run --project market_scout market-scout run --request-id first   # saves nearby menus; later checks read the copies

The second one fetches the competitors' own menu pages, so run it while online. `market-scout pages` lists what's saved.

From the repo root, start the server:

    uv run --project dashboard uvicorn main:app --app-dir dashboard/api --port 8000

In a second terminal, start the app, then open the address Vite prints (http://localhost:5173):

    cd dashboard/web
    pnpm install
    pnpm dev

The app sends `/api` requests to port 8000. To run everything from the server alone, run `pnpm build` once and open
http://127.0.0.1:8000.

## Pages

- **Dashboard**: start a check, then see the cash forecast, try what-ifs, and see the sponsor tools BackHouse runs on.
- **Forecast**: the charts, and what the forecast assumes.
- **Chat**: questions about the books, answered by ASI:One with the Bookkeeper's 11 read-only tools.
- **How it works**: each agent, in the order a check runs them.
- **Developer**: which agent calls which, live, with a log you can save as PDF or JSON.
