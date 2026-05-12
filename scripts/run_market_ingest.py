"""Entry-point for the prediction-markets ingest.

Usage:
    python scripts/run_market_ingest.py kalshi [--top-n N] [--status open]
    python scripts/run_market_ingest.py polymarket    # (phase 2)

Invoked by `osb-market-ingest-<platform>.timer` every 15 minutes.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("platform", choices=["kalshi", "polymarket"])
    p.add_argument("--top-n", type=int, default=200,
                   help="snapshot the top N markets by 24h volume (default 200)")
    p.add_argument("--status", default="open",
                   help="Kalshi status filter (open|closed|settled). default open.")
    args = p.parse_args()

    if args.platform == "kalshi":
        from markets.kalshi.ingest import run as run_kalshi
        result = run_kalshi(top_n=args.top_n, status=args.status)
        sys.exit(0 if result.get("markets_seen") else 1)

    if args.platform == "polymarket":
        print("polymarket ingest not implemented yet (phase 2)", flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
