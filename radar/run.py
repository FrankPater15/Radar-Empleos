"""Todo en uno: trae ofertas de todas las fuentes y puntúa con IA las más prometedoras.

Uso:  python -m radar.run              (ingesta + 30 puntuaciones)
      python -m radar.run --limit 60   (más puntuaciones; la capa gratis de Gemini va lento)
      python -m radar.run --no-score   (solo ingesta)

Pensado también para la tarea programada (pythonw.exe, sin consola): todo queda en logs/radar.log.
Sin red, espera un poco y termina con código 0 (no es un error: se reintenta mañana).
"""
from __future__ import annotations

import argparse
import sys

from radar import config
from radar.ingest import run as ingest
from radar.logsetup import setup_logging
from radar.netcheck import wait_for_network
from radar.score import ScoreSetupError, score_cli


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--sources", default=",".join(config.SOURCES))
    p.add_argument("--limit", type=int, default=30, help="ofertas a puntuar con IA")
    p.add_argument("--min-fit", type=int, default=config.MIN_FIT)
    p.add_argument("--no-score", action="store_true")
    p.add_argument("--log-file", default=str(config.LOG_FILE), help="archivo de log (con rotación)")
    p.add_argument("--wait-network", type=int, default=config.WAIT_NETWORK_S,
                   help="segundos máximos esperando red antes de rendirse (0 = no esperar)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    logger = setup_logging(args.log_file)
    log = logger.info
    try:
        log("=== Corrida del radar ===")
        if not wait_for_network(args.wait_network, log=log):
            logger.warning("Sin red: se omite la corrida de hoy")
            return 0

        errors: dict[str, str] = {}
        results = ingest([s.strip() for s in args.sources.split(",") if s.strip()], log=log, errors=errors)
        new = sum(n for n, _ in results.values())
        log(f"Total nuevas: {new}")
        if errors:
            logger.warning(f"Fuentes con error: {', '.join(errors)}")

        if not args.no_score:
            try:
                score_cli(config.CV_PATH, config.SCORING_PROVIDER, args.limit, min_fit=args.min_fit, log=log)
            except ScoreSetupError as e:
                logger.warning(f"Sin puntuación con IA: {e}")
            except Exception:
                logger.exception("La puntuación con IA falló; la ingesta sí quedó guardada")
        log("=== Fin de la corrida ===")
        return 0
    except Exception:
        logger.exception("Error inesperado en la corrida")
        return 1


if __name__ == "__main__":
    sys.exit(main())
