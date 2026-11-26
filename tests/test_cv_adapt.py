"""Fase 2b: CV adaptado. La IA es falsa; lo importante es que nada inventado llegue al PDF."""
from types import SimpleNamespace

import pytest

from radar import cv_adapt as ca
from radar.models import Job
from tests.test_pegar_descripcion import client  # noqa: F401  (fixture)
from tests.test_search import Session  # noqa: F401  (fixture)

CV = """## Perfil
Tecnólogo en Análisis y Desarrollo de Software (SENA).
## Habilidades técnicas
- Lenguajes: Python, JavaScript, SQL
- Backend: Node.js/Express, APIs REST, FastAPI
- Frontend: React
## Proyectos
### NailsSoft
- Stack: React, Node.js, MySQL
- Lo que hice: reduje el tiempo de agendamiento un 30 %
### Radar de Empleo CO
- FastAPI, SQLAlchemy, pytest
## Formación
- Tecnólogo en Análisis y Desarrollo de Software, SENA (2025)
## Idiomas
- Inglés: B1
"""


def tailored(**kw) -> ca.TailoredCV:
    base = dict(
        headline="Desarrollador Junior · Python y React",
        summary="Desarrollador junior con proyectos en Python y React.",
        skills=["Python", "React", "Kubernetes", "python", "APIs REST"],
        projects=[ca.Project(name="NailsSoft", stack="React, Node.js, Docker",
                             bullets=["Reduje el agendamiento un 30 %", "Atendí a 500 clientes"]),
                  ca.Project(name="Plataforma bancaria", bullets=["Lideré 10 personas"])],
        experience=[ca.Experience(role="Dev", org="Google", period="2020-2024", bullets=["Escalé servicios"])],
        education=["Tecnólogo en Análisis y Desarrollo de Software, SENA (2025)"],
        languages=["Inglés: B1"],
        gaps=["Docker"],
    )
    return ca.TailoredCV(**{**base, **kw})


def test_verify_removes_everything_not_in_master_cv():
    clean, removed = ca.verify(tailored(), CV)
    assert clean.skills == ["Python", "React", "APIs REST"]  # sin Kubernetes ni repetidas
    assert [p.name for p in clean.projects] == ["NailsSoft"]  # el proyecto inventado no pasa
    assert clean.projects[0].stack == "React, Node.js"  # Docker no está en el CV
    assert clean.projects[0].bullets == ["Reduje el agendamiento un 30 %"]  # 500 es una cifra inventada
    assert clean.experience == []  # nunca trabajó en Google
    assert "habilidad: Kubernetes" in removed and "proyecto: Plataforma bancaria" in removed


def test_invented_number_in_summary_is_dropped():
    clean, _ = ca.verify(tailored(summary="Junior con 3 años de experiencia. Me gusta Python."), CV)
    assert clean.summary == "Me gusta Python."


def test_contact_file_is_read_and_comments_ignored(tmp_path):
    f = tmp_path / "contacto.md"
    f.write_text("# Mis datos\nNombre: Ana Pérez\n- Correo: ana@example.com\nLinkedIn: linkedin.com/in/ana\n",
                 encoding="utf-8")
    assert ca.load_contact(f) == {"Nombre": "Ana Pérez", "Correo": "ana@example.com",
                                  "LinkedIn": "linkedin.com/in/ana"}
    assert ca.load_contact(tmp_path / "no-existe.md") == {}


def job(desc="Buscamos desarrollador junior con Python y React. Modalidad remota."):
    return Job(id="x:1", source="x", external_id="1", title="Dev Junior", company="Acme", url="https://a.co",
               description=desc)


def test_html_has_contact_and_language_of_the_offer():
    clean, _ = ca.verify(tailored(), CV)
    html = ca.render_html(clean, {"Nombre": "Ana Pérez", "Correo": "ana@example.com"}, job())
    assert "Ana Pérez" in html and "ana@example.com" in html and "Habilidades" in html
    assert "Kubernetes" not in html and "Docker" not in html  # los gaps no van al PDF
    en = ca.render_html(clean, {}, job("We need a junior developer with experience in Python and React."))
    assert "Skills" in en and "Tu nombre" in en


class FakeGemini:
    """Imita GeminiScorer: client.models.generate_content devuelve el JSON del CV."""

    def __init__(self, result: ca.TailoredCV):
        self.cv_text, self.model, self.fallback_model = CV, "m", None
        self.calls = []
        self.client = SimpleNamespace(models=SimpleNamespace(generate_content=self._gen))
        self._result = result

    def _gen(self, model, contents, config):
        self.calls.append((model, contents, config))
        return SimpleNamespace(text=self._result.model_dump_json())


def test_build_writes_verified_html(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "OUT_DIR", tmp_path)
    fake = FakeGemini(tailored())
    path, removed, gaps = ca.build(job(), fake, contact={"Nombre": "Ana"}, pdf=False)
    assert path.suffix == ".html" and "Kubernetes" not in path.read_text(encoding="utf-8")
    assert gaps == ["Docker"] and removed
    assert "nunca inventes" in fake.calls[0][2]["system_instruction"].lower()
    assert (tmp_path / "acme-dev-junior.json").exists()


def test_build_without_description_asks_to_paste_it():
    with pytest.raises(ValueError, match="descripción"):
        ca.build(job(desc=""), FakeGemini(tailored()), contact={}, pdf=False)


def test_panel_returns_pdf_and_reports_errors(client, monkeypatch, tmp_path):  # noqa: F811
    c, _Session, _scorer = client
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4 falso")
    monkeypatch.setattr(ca, "build", lambda job, scorer: (pdf, [], []))
    r = c.post("/jobs/getonbrd:1/cv", data={"back": "/"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert c.get("/jobs/getonbrd:1/cv.pdf").status_code == 404  # out_path real no existe

    def boom(job, scorer):
        raise ValueError("La oferta no tiene descripción")

    monkeypatch.setattr(ca, "build", boom)
    r = c.post("/jobs/getonbrd:1/cv", data={"back": "/"}, follow_redirects=False)
    assert r.status_code == 303 and "aviso=" in r.headers["location"]
