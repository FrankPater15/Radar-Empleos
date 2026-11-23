"""Puntuación de ofertas contra tu CV con IA (Gemini o Claude).  Uso:  python -m radar.score

La rúbrica se inspira en career-ops: nota global de 1 a 5, requisitos cumplidos y
faltantes, y una recomendación. Regla dura: la IA solo evalúa, nunca inventa experiencia.

Rúbrica v2: tope 2 para roles que no son de desarrollo, 1 si exige residir en otro país,
inglés del CV explícito, y `fit_pct`/`blockers` para desempatar. Gemini puede puntuar por
lotes (`score_batch`) con un presupuesto local de peticiones por minuto.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque
from typing import Literal

from pydantic import BaseModel

from radar import config
from radar.models import Job


class JobScore(BaseModel):
    score: int  # 1 a 5
    recommendation: Literal["postular", "tal vez", "no postular"]
    summary: str
    matched: list[str]
    missing: list[str]
    red_flags: list[str]
    fit_pct: int = 0  # 0-100, desempata dentro de la misma nota (rúbrica v2)
    blockers: list[str] = []  # requisitos que el candidato no puede cumplir (país, inglés, años…)


class BatchItem(JobScore):
    id: str  # id corto ("j1".."j6") de la oferta dentro del lote


class BatchScore(BaseModel):
    items: list[BatchItem]


SYSTEM_TEMPLATE = """Eres un reclutador técnico en Colombia que ayuda a un candidato a decidir a qué \
ofertas vale la pena postularse. Evalúas el encaje entre su CV y una oferta. Sé honesto: una \
nota inflada le hace perder tiempo y una muy baja le hace perder oportunidades.

Rúbrica de la nota global (entero de 1 a 5):
5 = cumple casi todos los requisitos obligatorios; postular hoy.
4 = cumple los obligatorios principales, faltan detalles aprendibles; postular.
3 = encaje parcial: le falta 1 requisito importante o la experiencia pedida es algo mayor; \
vale la pena si la empresa contrata juniors.
2 = faltan varios requisitos obligatorios o pide bastante más experiencia.
1 = otro perfil (p. ej. senior con 5+ años, otra especialidad).

Reglas duras (rúbrica {version}):
- Si el rol NO es de desarrollo, QA, automatización, RPA ni soporte TI de aplicaciones (p. ej. \
anotación de datos, idiomas, entrenamiento de IA, ventas, asistente), la nota máxima es 2.
- Si la oferta exige residencia o permiso de trabajo fuera de Colombia, o un horario nocturno en \
Colombia, la nota es 1.
- El candidato tiene inglés {english} (lo dice el CV). Si la oferta exige B2 o superior de forma \
explícita, la nota máxima es 2; si el inglés es solo deseable, réstalo en "fit_pct", no en la nota.
- El nivel "Semi Senior" de la ficha del portal no es una exigencia; usa lo que diga el texto.

Reglas:
- Usa SOLO lo que dice el CV. No supongas habilidades que no aparecen.
- Los años de experiencia pedidos cuentan: un perfil junior frente a "5+ años" es 1 o 2 aunque el stack coincida.
- Ten en cuenta ubicación y modalidad según las preferencias del CV.
- "matched": requisitos de la oferta que el CV demuestra (frases cortas).
- "missing": requisitos de la oferta que el CV no demuestra (frases cortas).
- "red_flags": señales de alerta reales de la oferta (pago no indicado no es alerta por sí solo; \
sí lo son pedir pagar por el proceso, pruebas técnicas que piden ejecutar código de terceros, \
contacto solo por Telegram, etc.). Lista vacía si no hay.
- "blockers": requisitos que el candidato NO puede cumplir hoy (residencia en otro país, inglés \
exigido, años, otra especialidad), en frases cortas en español. Lista vacía si no hay.
- "fit_pct": entero de 0 a 100 con el encaje fino, coherente con la nota (sirve para desempatar \
ofertas con la misma nota).
- "summary": 1 o 2 frases en español explicando la nota.
- "recommendation": "postular" para 4-5, "tal vez" para 3, "no postular" para 1-2.

<cv>
{cv}
</cv>"""


def system_prompt(cv_text: str) -> str:
    return SYSTEM_TEMPLATE.format(cv=cv_text, version=config.RUBRIC_VERSION, english=config.ENGLISH_LEVEL)


def cv_hash(cv_text: str) -> str:
    """Huella del CV + versión de la rúbrica: si cambia cualquiera, las notas quedan pendientes."""
    return hashlib.sha256((cv_text + config.RUBRIC_VERSION).encode("utf-8")).hexdigest()[:16]


_DROP_HEAD = re.compile(r"(?i)^\W*(about (?:us|the company|the team)|sobre nosotros|qui[eé]nes somos|acerca de|benefits?|"
                        r"beneficios|perks|what we offer|we offer|ofrecemos|lo que ofrecemos|equal (?:employment )?opportunit|"
                        r"eeo|privacy|aviso de privacidad|why (?:join|work)|por qu[eé] trabajar)")
_KEEP_HEAD = re.compile(r"(?i)^\W*(requirements?|requisitos|qualifications?|responsibilit|responsabilidades|funciones|"
                        r"what you.ll do|what you.ll need|what do you need|must have|you have|perfil|lo que buscamos|"
                        r"seniority|job location|ubicaci[oó]n|nice to have|deseable)")
_KEY_LINE = re.compile(r"(?i)\d\s*\+?\s*(?:(?:-|–|a|to)\s*\d+\s*)?(?:años|anos|years?|yrs|meses|months)|english|ingl[eé]s|"
                       r"resid|located|based in|authorized|countr|pa[ií]s|colombia|latam|shift|turno|horario|"
                       r"time ?zone|\best\b|\bpst\b|salary|salario|\busd\b|\bcop\b|remot|h[ií]brid|presencial|exclusiv")


def compact_description(text: str | None, limit: int = 1800) -> str:
    """Recorta la descripción para los lotes: quita "About us", beneficios, EEO y legales;
    prioriza Requisitos/Responsabilidades y conserva siempre las frases con años, inglés,
    país, horario, salario y modalidad. Mantiene el orden original de las líneas."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    ranked, mode = [], "neutral"
    for i, ln in enumerate(lines):
        if len(ln) <= 60 and _DROP_HEAD.search(ln):
            mode = "drop"
        elif len(ln) <= 60 and _KEEP_HEAD.search(ln):
            mode = "keep"
        key = bool(_KEY_LINE.search(ln))
        if mode == "drop" and not key:
            continue
        ranked.append((0 if key else 1 if mode == "keep" else 2, i, ln[:400]))
    chosen, used = [], 0
    for _prio, i, ln in sorted(ranked):
        if used + len(ln) + 1 > limit:
            continue
        chosen.append((i, ln))
        used += len(ln) + 1
    return "\n".join(ln for _, ln in sorted(chosen))


def _job_block(job: Job, description: str | None) -> str:
    salary = ""
    if job.min_salary or job.max_salary:
        salary = f"{job.min_salary or '?'}-{job.max_salary or '?'} {job.salary_currency or ''}/mes"
    return f"""Título: {job.title}
Empresa: {job.company or 'no indicada'}
Nivel según el portal: {job.seniority or 'no indicado'}
Categoría: {job.category or ''}
Modalidad: {job.remote_modality or ''}
Países: {', '.join(job.countries or [])}
Salario: {salary or 'no indicado'}

{description or '(sin descripción)'}"""


def job_prompt(job: Job) -> str:
    return f"""<oferta>
{_job_block(job, job.description)}
</oferta>

Evalúa esta oferta contra el CV."""


def batch_prompt(jobs_by_id: dict[str, Job], limit: int = 1800) -> str:
    blocks = "\n\n".join(f'<oferta id="{i}">\n{_job_block(j, compact_description(j.description, limit))}\n</oferta>'
                         for i, j in jobs_by_id.items())
    return f"""{blocks}

Evalúa CADA oferta contra el CV de forma independiente (no compares unas con otras). Devuelve \
"items" con un elemento por oferta y su "id" exacto ({', '.join(jobs_by_id)})."""


class ScoringError(RuntimeError):
    pass


def _clamp(result: JobScore) -> JobScore:
    result.score = max(1, min(5, result.score))
    result.fit_pct = max(0, min(100, result.fit_pct))
    return result


class Scorer:
    def __init__(self, cv_text: str, client=None, model: str | None = None, effort: str | None = None):
        if not cv_text.strip():
            raise ValueError("El CV está vacío")
        self.cv_text = cv_text
        self.cv_hash = cv_hash(cv_text)
        self.model = model or config.SCORING_MODEL
        self.effort = effort or config.SCORING_EFFORT
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        # El CV y la rúbrica son iguales en cada llamada: van en el system con caché
        self._system = [{
            "type": "text",
            "text": system_prompt(cv_text),
            "cache_control": {"type": "ephemeral"},
        }]

    def score(self, job: Job) -> JobScore:
        response = self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": self.effort},
            system=self._system,
            messages=[{"role": "user", "content": job_prompt(job)}],
            output_format=JobScore,
        )
        if response.stop_reason == "refusal":
            raise ScoringError(f"El modelo se negó a evaluar {job.id}")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise ScoringError(f"Respuesta incompleta para {job.id} (stop_reason={response.stop_reason})")
        return _clamp(response.parsed_output)


# Alias claro para cuando hay varios proveedores
ClaudeScorer = Scorer


class GeminiScorer:
    """Mismo contrato que Scorer, pero con la API de Gemini (tiene capa gratuita).

    Ojo: en la capa gratuita Google puede usar el contenido para mejorar sus productos,
    por eso el CV maestro no lleva teléfono, correo ni referencias.
    """

    def __init__(self, cv_text: str, client=None, model: str | None = None,
                 fallback_model: str | None = None, max_retries: int = 3, sleep=time.sleep, log=print,
                 rpm: int | None = None, primary_retry_every: int | None = None, clock=time.monotonic):
        if not cv_text.strip():
            raise ValueError("El CV está vacío")
        self.cv_text = cv_text
        self.cv_hash = cv_hash(cv_text)
        self.model = self.primary_model = model or config.GEMINI_MODEL
        self.fallback_model = fallback_model if fallback_model is not None else config.GEMINI_FALLBACK_MODEL
        self.max_retries = max_retries
        self._sleep = sleep
        self._log = log
        self._clock = clock
        self.rpm = config.GEMINI_RPM if rpm is None else rpm
        self.primary_retry_every = (config.GEMINI_PRIMARY_RETRY_EVERY if primary_retry_every is None
                                    else primary_retry_every)
        self._recent: deque[float] = deque()  # instantes de las últimas peticiones (presupuesto RPM)
        self._calls_on_fallback = 0
        if client is None:
            from google import genai

            client = genai.Client()  # lee GEMINI_API_KEY (o GOOGLE_API_KEY)
        self.client = client
        self._config = {
            "system_instruction": system_prompt(cv_text),
            "response_mime_type": "application/json",
            "response_json_schema": JobScore.model_json_schema(),
        }
        self._batch_config = {**self._config, "response_json_schema": BatchScore.model_json_schema()}

    def _throttle(self) -> None:
        """Presupuesto local de peticiones por minuto: espera antes de que Google responda 429."""
        if self.rpm <= 0:
            return
        now = self._clock()
        while self._recent and now - self._recent[0] >= 60:
            self._recent.popleft()
        if len(self._recent) >= self.rpm:
            wait = 60 - (now - self._recent[0])
            if wait > 0:
                self._sleep(wait)
            self._recent.popleft()
            now = self._clock()
        self._recent.append(now)

    def _maybe_back_to_primary(self) -> None:
        """El respaldo no se queda pegado: cada N llamadas se vuelve a probar el principal."""
        if self.model == self.primary_model or not self.primary_retry_every:
            return
        self._calls_on_fallback += 1
        if self._calls_on_fallback >= self.primary_retry_every:
            self._log(f"  vuelvo a probar {self.primary_model}")
            self.model = self.primary_model
            self._calls_on_fallback = 0

    def _generate(self, job: Job, contents: str | None = None, cfg: dict | None = None):
        from google.genai import errors

        self._maybe_back_to_primary()
        for attempt in range(self.max_retries + 1):
            try:
                self._throttle()
                return self.client.models.generate_content(
                    model=self.model, contents=contents or job_prompt(job), config=cfg or self._config
                )
            except errors.APIError as e:
                # 5xx = modelo saturado: cambia al de respaldo (y vuelve a probar el principal más tarde)
                if (e.code or 0) >= 500 and self.fallback_model and self.model != self.fallback_model:
                    self._log(f"  {self.model} no disponible ({e.code}); sigo con {self.fallback_model}")
                    self.model = self.fallback_model
                    self._calls_on_fallback = 0
                    continue
                # 429 = límite por minuto de la capa gratuita: esperar y reintentar
                if (e.code == 429 or (e.code or 0) >= 500) and attempt < self.max_retries:
                    self._sleep(min(60, 10 * 2 ** attempt))
                    continue
                raise ScoringError(f"Gemini falló con {job.id}: {e.code} {e.status}") from e
            except (ConnectionError, TimeoutError, OSError) as e:
                self._network_retry(job, attempt, e)
            except Exception as e:  # errores de red de httpx (p. ej. "Server disconnected")
                if type(e).__module__.split(".")[0] not in ("httpx", "httpx2", "httpcore"):
                    raise
                self._network_retry(job, attempt, e)
        raise ScoringError(f"Gemini no respondió para {job.id}")

    def _network_retry(self, job: Job, attempt: int, e: Exception) -> None:
        """Un corte de conexión no debe tumbar toda la corrida: espera y reintenta."""
        if attempt >= self.max_retries:
            raise ScoringError(f"Sin conexión con Gemini para {job.id}: {e}") from e
        self._sleep(min(60, 5 * 2 ** attempt))

    def score(self, job: Job) -> JobScore:
        response = self._generate(job)
        text = getattr(response, "text", None)
        if not text:
            reason = None
            if getattr(response, "candidates", None):
                reason = response.candidates[0].finish_reason
            raise ScoringError(f"Gemini no devolvió respuesta para {job.id} (finish_reason={reason})")
        try:
            result = JobScore.model_validate_json(text)
        except ValueError as e:
            raise ScoringError(f"JSON inválido de Gemini para {job.id}") from e
        return _clamp(result)

    def score_batch(self, jobs: list[Job], desc_limit: int = 1800) -> list[JobScore | ScoringError]:
        """Puntúa varias ofertas (~6) en una sola llamada, con la descripción recortada.

        Devuelve una lista alineada con `jobs`: JobScore o ScoringError. Las ofertas que no
        vuelvan en la respuesta (o vuelvan mal) se reintentan de a una con `score()`.
        """
        if not jobs:
            return []
        if len(jobs) == 1:
            return [self._safe_score(jobs[0])]
        by_id = {f"j{i + 1}": j for i, j in enumerate(jobs)}
        try:
            response = self._generate(jobs[0], batch_prompt(by_id, desc_limit), self._batch_config)
        except ScoringError as e:
            return [e] * len(jobs)  # la API falló tras los reintentos: no insistir oferta por oferta
        got: dict[str, JobScore] = {}
        try:
            raw = json.loads(getattr(response, "text", None) or "{}")
        except ValueError:
            raw = {}  # JSON inválido: todas se reintentan individualmente
        for item in raw.get("items", []) if isinstance(raw, dict) else []:
            try:
                parsed = BatchItem.model_validate(item)
            except ValueError:
                continue
            if parsed.id in by_id and parsed.id not in got:
                got[parsed.id] = _clamp(JobScore.model_validate(parsed.model_dump(exclude={"id"})))
        return [got[i] if i in got else self._safe_score(j) for i, j in by_id.items()]

    def _safe_score(self, job: Job) -> JobScore | ScoringError:
        try:
            return self.score(job)
        except ScoringError as e:
            return e


def make_scorer(cv_text: str, provider: str | None = None):
    provider = (provider or config.SCORING_PROVIDER).lower()
    if provider == "gemini":
        return GeminiScorer(cv_text)
    if provider in ("anthropic", "claude"):
        return ClaudeScorer(cv_text)
    raise ValueError(f"SCORING_PROVIDER desconocido: {provider} (usa gemini o anthropic)")
