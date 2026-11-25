"""Llena una base de demostración con ofertas ficticias.

Uso (PowerShell):
    $env:DATABASE_URL = "sqlite:///demo.db"; python scripts/demo.py
    uvicorn radar.main:app
Solo acepta una base llamada demo.db, para no tocar la tuya.
"""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from radar import config, db  # noqa: E402

if not config.DATABASE_URL.endswith("demo.db"):
    sys.exit("Define antes DATABASE_URL=sqlite:///demo.db")

from radar.models import Job, utcnow  # noqa: E402
from radar.relevance import fit  # noqa: E402

OFERTAS = [
    ("Desarrollador Full Stack Junior", "Empresa Ejemplo A", "Node.js, React, SQL, Git. Sin experiencia o 1 año.", 5, "encontrada"),
    ("Desarrollador RPA Junior", "Empresa Ejemplo B", "Python, RPA, OCR y APIs REST. 1 año de experiencia.", 5, "aprobada"),
    ("QA Automation Trainee", "Empresa Ejemplo C", "Testing, JavaScript, Git y CI/CD. Trainee.", 4, "enviada"),
    ("Backend Python Junior", "Empresa Ejemplo D", "Python, FastAPI, SQL, REST. Inglés B2 deseable.", 4, "encontrada"),
    ("Soporte de Aplicaciones N2", "Empresa Ejemplo E", "SQL, REST y atención a usuarios. 2 años.", 3, "entrevista"),
    ("Senior Cloud Architect", "Empresa Ejemplo F", "Más de 8 años de experiencia. Inglés C1.", 1, "descartada"),
]
TEXTO = {5: "Encaja con casi todos los requisitos.", 4: "Cumple lo principal; faltan detalles.",
         3: "Encaje parcial.", 1: "Perfil senior."}


def main() -> None:
    db.init_db(db.engine)
    ahora = utcnow()
    with db.SessionLocal() as s:
        for i, (titulo, empresa, desc, nota, estado) in enumerate(OFERTAS):
            j = Job(id=f"demo:{i}", source="demo", external_id=str(i), title=titulo, company=empresa,
                    url=f"https://example.com/oferta/{i}", category="Programming",
                    seniority="Senior" if nota == 1 else "Junior", remote_modality="fully_remote",
                    countries=["Colombia"], description=desc, open_to_colombia=True,
                    published_at=ahora - timedelta(hours=5 * i), status=estado, score=nota, scored_at=ahora,
                    score_data={"score": nota, "recommendation": "postular" if nota > 3 else "tal vez" if nota == 3 else "no postular",
                                "summary": TEXTO[nota], "matched": ["Git", "SQL"] if nota > 2 else [],
                                "missing": [] if nota == 5 else ["Más experiencia"], "red_flags": [],
                                "fit_pct": nota * 19, "blockers": []})
            j.fit, j.fit_reasons = fit(j)
            s.merge(j)
        s.commit()
    print(f"{len(OFERTAS)} ofertas ficticias listas")


if __name__ == "__main__":
    main()
