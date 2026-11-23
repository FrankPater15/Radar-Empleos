# Radar de Empleo CO

Agente personal de búsqueda de empleo para Colombia. Junta ofertas de fuentes **permitidas**, las guarda en una base de datos y las muestra en un panel donde decides qué hacer con cada una.

> **Human-in-the-loop y respeto de los ToS.** El radar **no** se postula solo ni hace scraping de LinkedIn ni de Computrabajo, porque sus términos lo prohíben. Busca, filtra y organiza. La postulación la haces tú.

## Estado: Fases 1, 2 y búsqueda ampliada

- [x] **3 fuentes con API pública:** Get on Board (6 categorías + búsquedas de texto), **Himalayas** (filtrada a ofertas abiertas a Colombia) y **Jobicy** (región LATAM)
- [x] **Prefiltro local de afinidad (0–100), sin IA:** nivel junior/senior, años de experiencia pedidos, tipo de rol, habilidades de tu CV, inglés exigido y ubicación. Cada punto deja una razón visible en el panel
- [x] La IA solo gasta cuota en las ofertas que pasan el prefiltro (`MIN_FIT`, por defecto 45), empezando por las mejores
- [x] **Sin duplicados entre fuentes:** la misma oferta (título + empresa normalizados) se guarda una sola vez
- [x] Comando todo en uno: `python -m radar.run`
- [x] Base de datos con SQLAlchemy (SQLite en local; Postgres/Neon cambiando una variable)
- [x] Ingesta idempotente: re-ejecutarla actualiza las ofertas, pero **nunca pisa el estado que tú marcaste**
- [x] Panel web (FastAPI + Jinja2): búsqueda, filtros por nivel, categoría, estado y "Colombia o 100 % remoto"
- [x] Embudo de estados: encontrada → aprobada → enviada → entrevista → oferta/rechazo (o descartada)
- [x] API JSON en `/api/jobs` (base para el panel React y la app Android)
- [x] Tests (pytest)
- [x] **Fase 2:** puntuación con IA (Gemini gratis o Claude) contra tu CV maestro: nota de 1 a 5, recomendación, requisitos cumplidos y faltantes, y señales de alerta (JSON estructurado validado con Pydantic)
- [x] Solo puntúa lo pendiente: las ofertas ya puntuadas no se repiten, y si cambias el CV se vuelven a puntuar
- [x] **Panel rediseñado:** anillo de afinidad, veredicto de la IA, razones del prefiltro, embudo con contadores, filtros por fuente y afinidad, botón *Deshacer*, tema claro/oscuro según el sistema y diseño adaptable a celular

## Hoja de ruta

| Fase | Contenido |
|---|---|
| 1 ✅ | Get on Board, base de datos y panel con filtros y estados |
| 1b | Lectura de **tus propios correos de alertas** (Computrabajo, LinkedIn) con la API de Gmail (OAuth, solo lectura) |
| 2 ✅ | Puntuación con LLM (nota de 1 a 5, requisitos cumplidos y faltantes, en JSON) |
| 2b | CV adaptado por oferta en PDF. **Regla: nunca inventar experiencia** |
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

python -m radar.run           # trae ofertas de las 3 fuentes y puntúa con IA las 30 más prometedoras
uvicorn radar.main:app --reload
```

Abre http://127.0.0.1:8000.

### Puntuación con IA (fase 2)

1. Copia `cv/cv_maestro.example.md` a `cv/cv_maestro.md` y complétalo con datos reales. Git lo ignora, así que no se sube.
2. Elige un proveedor en `.env`:
   - **Gemini (por defecto, gratis):** crea la key en https://aistudio.google.com y ponla como `GEMINI_API_KEY=...`. En la capa gratuita Google puede usar lo que envías para mejorar sus productos, por eso el CV maestro no lleva teléfono, correo ni referencias.
   - **Claude (de pago):** pon `SCORING_PROVIDER=anthropic` y `ANTHROPIC_API_KEY=...`. La API se paga aparte de Claude Pro.
3. Ejecuta:

```powershell
python -m radar.score --limit 10     # empieza con pocas para ver el costo
```

**Opciones:** `--rescore` vuelve a puntuar todo; `--all-countries` incluye ofertas no abiertas a Colombia.

**Cómo funciona:**
- **Misma rúbrica en los dos proveedores:** el CV y la rúbrica van en las instrucciones del sistema y la oferta en el mensaje, y la respuesta es un JSON validado con Pydantic.
- **Gemini:** usa `gemini-3.8-flash`. Si está saturado (503), cambia a `gemini-3.1-flash-lite` durante el resto de la corrida. Ante el límite por minuto (429) espera y reintenta. Cada nota guarda qué modelo la generó.
- **Claude:** usa `claude-opus-5-5` con `effort=low`, caché de prompts para el CV y fallback del servidor si el modelo rechaza la petición.

Opciones de la ingesta:

```powershell
python -m radar.ingest                      # solo descarga, de todas las fuentes
python -m radar.ingest --sources himalayas  # una sola fuente
python -m radar.ingest --refit              # recalcula el prefiltro (p. ej. tras cambiar SKILLS)
python -m radar.run --limit 60 --min-fit 60 # más puntuaciones, solo las muy afines
```

Las búsquedas (`SEARCH_QUERIES`), tus habilidades (`SKILLS`) y la afinidad mínima (`MIN_FIT`) se cambian en `.env`.

Tests:

```powershell
pytest
```

## Fuente ATS: empresas con tablero público

`radar/sources/ats.py` lee los tableros públicos de empleo (Greenhouse, Lever, Ashby, SmartRecruiters,
Workable y BambooHR) de las empresas listadas en `radar/sources/ats_companies.json`. Solo guarda las
ofertas abiertas a Colombia (país CO, ciudad colombiana, LATAM o remoto desde cualquier lugar) y enlaza
a la oferta original. Se desactiva con `ATS_ENABLED=0`. RemoteOK está disponible como fuente opcional:
`python -m radar.ingest --sources remoteok` (cita "Remote OK" y enlaza a su URL).

Para añadir una empresa, agrega una entrada `{"name": "...", "ats": "...", "token": "..."}`:

| ats | token | ejemplo |
|---|---|---|
| `greenhouse` | slug de `job-boards.greenhouse.io/{token}` | `canonical` |
| `lever` | slug de `jobs.lever.co/{token}` | `tryjeeves` |
| `ashby` | slug de `jobs.ashbyhq.com/{token}` | `addi` |
| `smartrecruiters` | id de la empresa en `jobs.smartrecruiters.com/{token}` | `boschgroup` |
| `workable` | subdominio de `apply.workable.com/{token}` | `platzi` |
| `bamboohr` | subdominio de `{token}.bamboohr.com` | `vozy` |

Antes de añadirla, comprueba que responde y tiene ofertas para Colombia:
`python -c "from radar.sources import ats; from radar.sources.common import make_client; print(len(ats.fetch_company(make_client(), {'name':'X','ats':'lever','token':'slug'})))"`.
Hay una pausa de 1 s entre empresas y cada una falla por separado.

## Arquitectura

```
radar/
  config.py            variables de entorno
  db.py                engine + sesión SQLAlchemy
  models.py            modelo Job y estados del embudo
  sources/             getonbrd.py, himalayas.py, jobicy.py + common.py (reintentos con Retry-After, HTML a texto)
  relevance.py         prefiltro de afinidad 0–100 con razones legibles
  run.py               ingesta + puntuación en un solo comando
  ingest.py            upsert idempotente (no toca status ni first_seen_at) + control de duplicados
  scoring.py           rúbrica + GeminiScorer / ClaudeScorer con salida estructurada (JobScore)
  score.py             CLI: puntúa las pendientes y guarda una por una
  main.py              FastAPI: panel HTML, /api/jobs, cambio de estado
  templates/, static/  vista
tests/                 tests de parser, upsert y endpoints, con una respuesta real de la API como fixture
```

**Decisiones:**
- El id de cada oferta es `fuente:id_externo`. Así varias fuentes conviven sin chocar.
- "Abierta a Colombia": en Get on Board, oferta en Colombia o 100 % remota; en Himalayas y Jobicy, Colombia, LATAM o "cualquier país" entre los lugares permitidos.
- Los salarios se muestran en USD/mes; los anuales (Jobicy, algunos de Himalayas) se dividen entre 12.
- Jobicy y Himalayas piden citarlos como fuente y enlazar a la oferta original: el panel muestra la fuente y el botón *Ver oferta* abre la publicación original.
- El prefiltro es deliberadamente simple y explicable; la nota fina la pone la IA.
