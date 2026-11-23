# Radar de Empleo CO

Agente personal de búsqueda de empleo para Colombia. Junta ofertas de fuentes **permitidas**, las guarda en una base de datos y las muestra en un panel donde decides qué hacer con cada una.

> **Human-in-the-loop y respeto de los ToS.** El radar **no** se postula solo ni hace scraping de LinkedIn ni de Computrabajo, porque sus términos lo prohíben. Busca, filtra y organiza. La postulación la haces tú.

## Estado: Fase 1 (MVP)

- [x] Ingesta desde la **API pública oficial de Get on Board** (categorías `programming`, `technical-support`, `sysadmin-devops-qa`)
- [x] Base de datos con SQLAlchemy (SQLite en local; Postgres/Neon cambiando una variable)
- [x] Ingesta idempotente: re-ejecutarla actualiza las ofertas, pero **nunca pisa el estado que tú marcaste**
- [x] Panel web (FastAPI + Jinja2): búsqueda, filtros por nivel, categoría, estado y "Colombia o 100 % remoto"
- [x] Embudo de estados: encontrada → aprobada → enviada → entrevista → oferta/rechazo (o descartada)
- [x] API JSON en `/api/jobs` (base para el panel React y la app Android)
- [x] Tests (pytest)

## Hoja de ruta

| Fase | Contenido |
|---|---|
| 1 ✅ | Get on Board, base de datos y panel con filtros y estados |
| 1b | Lectura de **tus propios correos de alertas** (Computrabajo, LinkedIn) con la API de Gmail (OAuth, solo lectura) |
| 2 | Puntuación con LLM (nota de 1 a 5, requisitos cumplidos y faltantes, en JSON) y CV adaptado en PDF. **Regla: nunca inventar experiencia** |
| 3 | Login JWT, WebSockets ("nueva oferta 4,5/5" en tiempo real), métricas del embudo |
| 4 | App Android (push + Aprobar/Descartar), despliegue en Render/Railway + Neon, video de demo |

## Requisitos

- Python 3.11 o superior

## Instalación y uso (Windows / PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env        # opcional

python -m radar.ingest        # trae ofertas (puedes repetirlo cuando quieras)
uvicorn radar.main:app --reload
```

Abre http://127.0.0.1:8000.

Opciones de la ingesta:

```powershell
python -m radar.ingest --categories programming,technical-support --max-pages 3
```

Tests:

```powershell
pytest
```

## Arquitectura

```
radar/
  config.py            variables de entorno
  db.py                engine + sesión SQLAlchemy
  models.py            modelo Job y estados del embudo
  sources/getonbrd.py  cliente de la API: paginación, pausa entre peticiones, HTML a texto
  ingest.py            upsert idempotente (no toca status ni first_seen_at)
  main.py              FastAPI: panel HTML, /api/jobs, cambio de estado
  templates/, static/  vista
tests/                 tests de parser, upsert y endpoints, con una respuesta real de la API como fixture
```

**Decisiones:**
- El id de cada oferta es `fuente:id_externo`. Así varias fuentes conviven sin chocar.
- "Abierta a Colombia" significa que la oferta es en Colombia o es 100 % remota.
- Los salarios de Get on Board se muestran en USD/mes, tal como los publica la fuente.
