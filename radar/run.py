"""Todo en uno: trae ofertas de todas las fuentes y puntúa con IA las más prometedoras.

Uso:  python -m radar.run              (ingesta + 30 puntuaciones)
      python -m radar.run --limit 60   (más puntuaciones; la capa gratis de Gemini va lento)
      python -m radar.run --no-score   (solo ingesta)
"""
from __future__ import annotations

import argparse
import sys

from radar import config
from radar.ingest import run as ingest
from radar.score import score_cli


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sources", default=",".join(config.SOURCES))
    p.add_argument("--limit", type=int, default=30, help="ofertas a puntuar con IA")
    p.add_argument("--min-fit", type=int, default=config.MIN_FIT)
    p.add_argument("--no-score", action="store_true")
    args = p.parse_args()

    results = ingest([s.strip() for s in args.sources.split(",") if s.strip()])
    print(f"Total nuevas: {sum(n for n, _ in results.values())}")
    if not args.no_score:
        score_cli(config.CV_PATH, config.SCORING_PROVIDER, args.limit, min_fit=args.min_fit)


if __name__ == "__main__":
    main()
