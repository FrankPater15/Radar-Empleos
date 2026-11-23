"""Puntuación de ofertas contra tu CV con Claude.  Uso:  python -m radar.score

La rúbrica se inspira en career-ops: nota global de 1 a 5, requisitos cumplidos y
faltantes, y una recomendación. Regla dura: la IA solo evalúa, nunca inventa experiencia.
"""
from __future__ import annotations

import hashlib
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

Reglas:
- Usa SOLO lo que dice el CV. No supongas habilidades que no aparecen.
- Los años de experiencia pedidos cuentan: un perfil junior frente a "5+ años" es 1 o 2 aunque el stack coincida.
- Ten en cuenta ubicación y modalidad según las preferencias del CV.
- "matched": requisitos de la oferta que el CV demuestra (frases cortas).
- "missing": requisitos de la oferta que el CV no demuestra (frases cortas).
- "red_flags": señales de alerta reales de la oferta (pago no indicado no es alerta por sí solo; \
sí lo son pedir pagar por el proceso, pruebas técnicas que piden ejecutar código de terceros, \
contacto solo por Telegram, etc.). Lista vacía si no hay.
- "summary": 1 o 2 frases en español explicando la nota.
- "recommendation": "postular" para 4-5, "tal vez" para 3, "no postular" para 1-2.

<cv>
{cv}
</cv>"""


def cv_hash(cv_text: str) -> str:
    return hashlib.sha256(cv_text.encode("utf-8")).hexdigest()[:16]


def job_prompt(job: Job) -> str:
    salary = ""
    if job.min_salary or job.max_salary:
        salary = f"{job.min_salary or '?'}-{job.max_salary or '?'} {job.salary_currency or ''}/mes"
    return f"""<oferta>
Título: {job.title}
Empresa: {job.company or 'no indicada'}
Nivel según el portal: {job.seniority or 'no indicado'}
Categoría: {job.category or ''}
Modalidad: {job.remote_modality or ''}
Países: {', '.join(job.countries or [])}
Salario: {salary or 'no indicado'}

{job.description or '(sin descripción)'}
</oferta>

Evalúa esta oferta contra el CV."""


class ScoringError(RuntimeError):
    pass


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
            "text": SYSTEM_TEMPLATE.format(cv=cv_text),
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
        result: JobScore = response.parsed_output
        result.score = max(1, min(5, result.score))
        return result
