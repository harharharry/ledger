"""Dashboard serve command: refresh data.json, then serve dashboard/ locally.

Usage:
    python -m ledger.dashboard              # fetch live data, export, serve
    python -m ledger.dashboard --no-fetch   # serve existing data.json as-is

Localhost only. The single write endpoint is the kill switch (POST
/api/kill-switch {"engaged": bool}) — it creates or removes the KILL_SWITCH
file, the same manual override `touch KILL_SWITCH` provides. Everything else
is static. Phase 2's approve/decline flow will extend this server.
"""

from __future__ import annotations

import argparse
import json
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import kill_switch
from .config import load_config
from .dashboard_data import build_dashboard_data, write_dashboard_data
from .data import alpaca, coingecko, fx
from .data.alpaca import MissingCredentialsError
from .paper_ledger import PaperLedger

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = ROOT / "dashboard"


def export(config, ks_path: Path) -> None:
    fx_rate = fx.fetch_gbp_usd()
    series = {}
    for asset in config.sleeve_assets("crypto"):
        series[asset.symbol] = coingecko.fetch_daily_closes(asset.symbol, asset.coingecko_id)
    for asset in config.sleeve_assets("stocks"):
        try:
            series[asset.symbol] = alpaca.fetch_daily_closes(asset.symbol)
        except MissingCredentialsError as e:
            print(f"WARNING: {asset.symbol} omitted — {e}", file=sys.stderr)
    with PaperLedger(ROOT / config.runtime.db_path) as led:
        data = build_dashboard_data(
            led, config, series, fx_rate,
            kill_switch_engaged=kill_switch.is_engaged(ks_path),
        )
    write_dashboard_data(data, DASHBOARD_DIR / "data.json")
    print(f"exported {DASHBOARD_DIR / 'data.json'}")


class Handler(SimpleHTTPRequestHandler):
    ks_path: Path  # set via partial in main()

    def do_POST(self):
        if self.path != "/api/kill-switch":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if body.get("engaged"):
            kill_switch.engage(self.ks_path, reason="engaged from dashboard")
        else:
            kill_switch.disengage(self.ks_path)
        payload = json.dumps({"engaged": kill_switch.is_engaged(self.ks_path)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass  # keep the terminal quiet; errors still raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--no-fetch", action="store_true",
                        help="serve the existing data.json without hitting APIs")
    args = parser.parse_args(argv)

    config = load_config(ROOT / "config.toml")
    ks_path = ROOT / config.runtime.kill_switch_path

    if not args.no_fetch:
        try:
            export(config, ks_path)
        except Exception as e:
            print(f"EXPORT FAILED: {type(e).__name__}: {e}", file=sys.stderr)
            return 1

    handler = partial(Handler, directory=str(DASHBOARD_DIR))
    handler.ks_path = ks_path
    Handler.ks_path = ks_path
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"dashboard: http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
