"""Prefiltro local (sin IA, gratis e instantáneo): nota de afinidad 0–100 para cada oferta.

Sirve para tres cosas:
  1. ordenar el panel aunque una oferta todavía no tenga nota de IA;
  2. gastar la cuota de Gemini solo en las ofertas que valen la pena (score.py usa MIN_FIT);
  3. calcular el orden final del panel con `rank_value` (IA + prefiltro + bloqueadores + antigüedad).

Las reglas buscan señales de nivel (junior vs. senior), años de experiencia exigidos,
tipo de rol (técnico o no), habilidades del CV, idioma y ubicación. Cada ajuste deja una
razón legible, que el panel muestra como etiqueta.

Reglas v2 (validadas contra radar.db, ver tests/test_ranking_v2.py): trabajos de anotación o
idiomas para IA −40, otros roles no técnicos −25, oferta restringida a otro país −35, inglés
alto −20 / intermedio −8 (el CV dice A2), los rangos de años se leen por el mínimo, y los
avisos multinivel o "associate/entry-level" cuentan como junior.
"""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timezone

from radar import config


def _norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _rx(words: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9])(?:{words})(?![a-z0-9])")


JUNIOR = _rx(r"juniors?|jr\.?|trainees?|interns?|internships?|pasantes?|pasantias?|practicantes?|practicas?|"
             r"aprendices|aprendiz|apprentices?|entry[- ]level|graduate|recien egresado|sin experiencia|semillero|nivel inicial|level i")
SEMI = _rx(r"semi[- ]?senior|semi[- ]?sr|ssr|mid[- ]?level|intermedio|nivel ii|level ii")
SENIOR = _rx(r"senior|sr\.?|lead|lider|tech lead|staff|principal|head|director|manager|gerente|"
             r"arquitecto|architect|expert|experto|vp")
TECH_ROLE = _rx(r"database|dba|salesforce|mulesoft|sap|erp|crm|security|seguridad|infosec|"
                r"ciberseguridad|machine learning|ml|ai|ia|cloud|desarrollador(?:a)?|developer|dev|engineer|ingenier[oa]|programador(?:a)?|software|"
                r"full[- ]?stack|backend|back[- ]end|frontend|front[- ]end|qa|tester|testing|automation|"
                r"automatizacion|automatizaciones|rpa|soporte|support|implementacion|implementation|"
                r"sistemas|it|ti|web|mobile|flutter|python|java|javascript|react|node|angular|\.net|"
                r"data|datos|devops|integraciones|integration|aplicaciones|technical|tecnico")
NON_TECH = _rx(r"sales|ventas|vendedor|account executive|ejecutiv[oa]|marketing|paid media|media buyer|"
               r"ad ops|ads|seo|recruiter|reclutador|talent|hr|rrhh|people|finance|finanzas|contador|"
               r"accountant|legal|lawyer|attorney|paralegal|abogado|nurse|doctor|medico|teacher|profesor|tutor|clinical|copywriter|content|linguist|linguistic|"
               r"translator|traductor|customer service|call center|telemarketing|designer|disenador|"
               r"product owner|business analyst|analista de negocios|onboarding|success|"
               # v3: legal, cumplimiento, nómina, reclutamiento y construcción (pasaban como "poco claro")
               r"counsel|compliance|cumplimiento|payroll|nomina|benefits|beneficios|compensacion|"
               r"compensation|talent acquisition|ta|construction|construccion")
# v2: otros no técnicos que el regex anterior dejaba pasar como "rol técnico" o "poco claro"
NON_TECH_EXTRA = _rx(r"assistant|asistente|bookkeep\w*|sdr|business development|administrative|"
                     r"virtual assistant|estimator|coordinator|coordinador(?:a)?|recruit\w*|design|diseno|graphic|tax|"
                     r"bookkeeper|auditor\w*")
# v2: anotación de datos, idiomas o entrenamiento de IA (no son roles de desarrollo)
GIG = _rx(r"annotat\w*|data contributor|ai trainer|ai tutor|freelance ai|labell?ing|rlhf|linguist\w*|"
          r"language (?:alignment|data|expert)|talent pool|ai community|transcri\w+|rater|evaluator")
# Títulos claramente de desarrollo: ganan aunque el título tenga una palabra "no técnica"
CORE_DEV = _rx(r"desarrollador(?:a|es)?|developers?|programador(?:a|es)?|software engineers?|ingenier[oa] de software|"
               r"full[- ]?stack|backend|back[- ]end|frontend|front[- ]end|qa|automation|automatizacion|rpa|"
               r"analista desarrollador|tester|android|ios")
ASSOCIATE = _rx(r"associate|asociado|ayudante|auxiliar")
REQ_CODE = re.compile(r"(?<![a-z0-9])(?:jr|req|r)-?\d{3,}")
# "3 años", "3+ years", "1-3 años", "2 a 4 years": el grupo 2 es el máximo del rango
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|a|to)\s*(\d{1,2})\s*)?(?:\+\s*)?(?:anos?|years?|yrs)(?![a-z])")
# Aviso multinivel ("Junior 0–2 years … Mid … Senior") o señal explícita de entrada
MULTI_JR = re.compile(r"junior\W{0,5}\(?\s*0\s*(?:-|–|a|to)\s*[12]\s*(?:anos?|years?)|early[- ]career|"
                      r"entry[- ]level|new grad|recien egresad")
ENGLISH_HIGH = _rx(r"c1|c2|b2\+?|fluent english|english fluency|fluent in english|advanced english|"
                   r"ingles avanzado|ingles fluido|bilingue|bilingual|native english|"
                   r"excellent english|strong english|full professional proficiency")
ENGLISH_MID = _rx(r"good english|english (?:level:? )?(?:b1|b2)|intermediate english|conversational english|"
                  r"upper[- ]intermediate|ingles (?:intermedio|conversacional|b1|b2)|english.{0,25}(?:required|requerido|indispensable)")
ENGLISH_LOW = _rx(r"ingles (?:deseable|basico|no excluyente|opcional)|english (?:is a plus|nice to have)|basic english")
B1_B2 = re.compile(r"b1\s*(?:-|–|/|a|to)\s*b2")  # "B1–B2" es intermedio, no alto
# Filipinas solo cuenta si el aviso exige residir/trabajar/ser de allá: una mención suelta
# ("tenemos oficinas en Filipinas, EE. UU. y Brasil") no restringe la oferta.
_PH = r"(?:the )?(?:philippines|filipin\w+)"
_PHILIPPINES = (rf"(?:based|located|residing|living|resid\w+|work\w*|hiring|available|open|candidates?|applicants?)"
                rf"\s+(?:\w+\s+){{0,3}}?(?:in|from|to|within)\s+{_PH}|"
                rf"{_PH}[- ]based|philippines? (?:only|residents?|nationals?)|"
                rf"filipino (?:nationals?|citizens?|residents?|locals?|candidates?|applicants?|only)|"
                rf"exclusiv\w*(?: only)? (?:to|for) (?:filipino|the philippines)|"
                rf"(?:citizens?|nationals?) of {_PH}|(?:residentes?|ubicad\w+) en filipinas")
LOC_RESTRICTED = re.compile(
    r"residents? of|must (?:be )?(?:reside|live|located|based) in|"
    r"only (?:open|available) to (?:candidates|residents) (?:in|from)|"
    r"exclusiv\w+ (?:para|for|to) (?:residentes|residents)|(?:us|u\.s\.)[- ]based only|"
    r"must be (?:a )?(?:us|u\.s\.) citizen|authorized to work in the (?:us|united states|uk|eu)|"
    r"india only|based in (?:india|europe|the uk)|" + _PHILIPPINES)
OPEN_TO_US = re.compile(r"latam|latin america|latinoamerica|south america|americas|colombia")
COUNTRY_NAMES = [
    ("Filipinas", re.compile(r"philippines|filipin")), ("EE. UU.", re.compile(r"(?<![a-z])(?:us|u\.s\.|united states|usa)(?![a-z])")),
    ("Reino Unido", re.compile(r"(?<![a-z])(?:uk|united kingdom)(?![a-z])")), ("India", re.compile(r"india")),
    ("Europa", re.compile(r"(?<![a-z])(?:eu|europe)(?![a-z])")), ("Canadá", re.compile(r"canada")),
    ("México", re.compile(r"mexico")), ("Brasil", re.compile(r"brazil|brasil")), ("Argentina", re.compile(r"argentina")),
]
NIGHT = _rx(r"night shift|graveyard|overnight|turno nocturno|horario nocturno")
TZ_US = _rx(r"est|edt|pst|pdt|cst|cdt|us hours|us business hours|us time ?zones?|eastern time|pacific time|central time")
TZ_EU = _rx(r"uk hours|gmt|bst|cet|cest|european hours")
TZ_ASIA = _rx(r"aest|sgt|ist|manila time|philippine time|apac hours")
HYBRID = _rx(r"hybrid|hibrido|hibrida")
ONSITE = _rx(r"on[- ]?site|presencial|in[- ]office")
REMOTE = _rx(r"remote|remoto|remota|teletrabajo|home office")
SALARY_TXT = re.compile(
    r"(usd|cop|us\$|\$|eur|€|php)\s*(\d[\d.,]*)\s*(k|mil)?\s*(?:usd|cop)?\s*(?:-|–|a|to)\s*(?:usd|cop|us\$|\$|eur|€|php)?\s*"
    r"(\d[\d.,]*)\s*(k|mil)?\s*(usd|cop|eur)?")
# Señales de estafa. Muchos avisos legítimos dicen "nunca te pediremos pagar ni escribir por
# Telegram": por eso se ignoran las coincidencias con una negación cerca y antes en la frase.
SCAM = [
    ("pide pagar por el proceso", re.compile(r"(?:pay|pagar|paga|deposit\w*|deposita\w*|fee|cuota|abonar)\W+(?:\w+\W+){0,4}?"
                                             r"(?:training|application|registration|kit|course|curso|inscripcion|registro|capacitacion|equipment|equipo)")),
    ("contacto solo por Telegram/WhatsApp", re.compile(r"telegram|whatsapp")),
    ("cobro por cursos o exámenes", re.compile(r"(?:costo|valor|pago|cobro)\W+(?:\w+\W+){0,3}?(?:del? )?(?:curso|examen|examenes|certificacion)")),
    ("correo @gmail para postular", re.compile(r"[a-z0-9._-]+@(?:gmail|hotmail|outlook|yahoo)\.com")),
    ("\"sin requisitos\"", re.compile(r"sin requisitos|no requirements needed|no experience or skills")),
    ("pago con tarjetas regalo o cheques", re.compile(r"gift cards?|tarjetas? de regalo|check to purchase|cheque para comprar")),
]
NEGATION = re.compile(r"(?<![a-z])(?:never|nunca|not|no|won'?t|will not|don'?t|do not|jamas|ni|cannot|beware|cuidado|fraud\w*|scam\w*|estafa\w*)(?![a-z])")
_WORDS = {"un": "1", "uno": "1", "una": "1", "one": "1", "dos": "2", "two": "2", "tres": "3", "three": "3",
          "cuatro": "4", "four": "4", "cinco": "5", "five": "5", "seis": "6", "six": "6"}
NUM_WORDS = _rx("|".join(_WORDS))
MONTHS = re.compile(r"(?<!\d)(?:[1-9]|1[01])\s*(?:\+\s*)?(?:meses|months)\s+(?:de\s+|of\s+)?(?:experiencia|experience)")
STUDENT_REQ = re.compile(r"(?:ser|be|must be|debe ser|currently)\s+(?:un\s+|a\s+)?(?:estudiante|student|enrolled)|"
                         r"estudiantes? (?:activo|de ultimos semestres)|convenio (?:de practicas|universitario)|"
                         r"carta de presentacion de la universidad")
SPANISH_POST = _rx(r"requisitos|experiencia|conocimientos|buscamos|ofrecemos|funciones|"
                   r"responsabilidades|desarrollador")
LOCAL = _rx(r"medellin|antioquia|envigado|itagui|sabaneta|bello|valle de aburra|rionegro")

_ENGLISH_RANK = {"a1": 1, "a2": 2, "b1": 3, "b2": 4, "c1": 5, "c2": 6}


def _english_rank() -> int:
    return _ENGLISH_RANK.get(_norm(getattr(config, "ENGLISH_LEVEL", "A2")), 2)


def _skills() -> list[tuple[str, re.Pattern]]:
    return [(s, _rx(re.escape(_norm(s)))) for s in config.SKILLS]


_SKILL_RX = None


def _getter(job):
    return (lambda k: job.get(k)) if isinstance(job, dict) else (lambda k: getattr(job, k, None))


# Fin de frase: . ; ! ? o salto de línea, salvo el punto de una sigla ("U.S.", "e.g.", "i.e.")
_SENT_END = re.compile(r"(?<!(?<![a-z])[a-z])\.|[\n;!?]")


def _sentence(text: str, start: int, end: int) -> str:
    a = -1
    for m in _SENT_END.finditer(text, 0, start):
        a = m.start()
    b = _SENT_END.search(text, end)
    return text[a + 1:b.start() if b else len(text)]


def _country_restriction(body: str) -> str | None:
    for m in LOC_RESTRICTED.finditer(body):
        sent = _sentence(body, m.start(), m.end())
        if OPEN_TO_US.search(sent):
            continue  # "residents of LATAM", "must be based in Colombia or Mexico": abierta a ti
        window = body[m.start():m.end() + 60]
        for name, rx in COUNTRY_NAMES:
            if rx.search(window):
                return name
        return "otro país"
    return None


def _scam_signals(text: str) -> list[str]:
    found = []
    for label, rx in SCAM:
        for m in rx.finditer(text):
            sent = _sentence(text, m.start(), m.end())
            before = text[max(text.rfind(c, 0, m.start()) for c in ".\n;!?") + 1:m.start()]
            if NEGATION.search(before[-80:]):
                continue  # "nunca te pediremos pagar", "we will never contact you on Telegram"
            if label.startswith("contacto") and not re.search(r"only|solo|unicamente|exclusiv", sent):
                continue  # mencionar WhatsApp no es alerta; "contacto solo por WhatsApp" sí
            found.append(label)
            break
    return found


def _money(num: str, mult: str | None) -> int | None:
    digits = num.replace(",", "").replace(".", "") if re.search(r"[.,]\d{3}(?!\d)", num) else num.replace(",", "")
    try:
        value = float(digits)
    except ValueError:
        return None
    if mult == "k":
        value *= 1000
    elif mult == "mil":
        value *= 1000
    return int(value)


def _salary(get, body: str) -> dict | None:
    if get("min_salary") or get("max_salary"):
        return {"min": get("min_salary"), "max": get("max_salary"),
                "moneda": get("salary_currency") or "?", "fuente": "portal"}
    m = SALARY_TXT.search(body)
    if not m:
        return None
    cur_a, n1, k1, n2, k2, cur_b = m.groups()
    lo, hi = _money(n1, k1 or k2), _money(n2, k2)
    if not lo or not hi:
        return None
    cur = {"$": "USD", "us$": "USD", "€": "EUR"}.get(cur_a, cur_a).upper()
    if cur_b:
        cur = cur_b.upper()
    return {"min": lo, "max": hi, "moneda": cur, "fuente": "texto"}


_REQ_CACHE: dict[tuple, dict] = {}
_REQ_CACHE_MAX = 8192


def _clone_req(req: dict) -> dict:
    """Copia con las listas y dicts anidados propios: quien reciba el resultado puede modificarlo."""
    return {**req, "alertas_estafa": list(req["alertas_estafa"]), "bloqueadores": list(req["bloqueadores"]),
            "salario": dict(req["salario"]) if req["salario"] else None}


def extract_requirements(job) -> dict:
    """Requisitos de la oferta (ver `_extract_requirements`), con caché en memoria.

    El orden "mejor" del panel lo calcula por cada oferta en cada petición (~2 s con 1.500
    ofertas) y las regex no cambian mientras la oferta no cambie: la clave incluye el contenido
    que lee la extracción (título, descripción, modalidad, salario) y la configuración de la que
    depende (nivel de inglés, estudiante), así que editar la oferta o el CV invalida sola.
    """
    get = _getter(job)
    desc = get("description")
    key = (get("id"), get("title"), hash(desc) if desc else 0, len(desc or ""), get("remote_modality"),
           get("min_salary"), get("max_salary"), get("salary_currency"),
           getattr(config, "ENGLISH_LEVEL", None), getattr(config, "IS_STUDENT", None))
    try:
        hit = _REQ_CACHE.get(key)
    except TypeError:  # algún campo no hashable: sin caché
        return _extract_requirements(job)
    if hit is None:
        if len(_REQ_CACHE) >= _REQ_CACHE_MAX:
            _REQ_CACHE.clear()
        hit = _REQ_CACHE[key] = _extract_requirements(job)
    return _clone_req(hit)


EXPERIENCE_NEAR = 70  # caracteres entre "N años" y la palabra experiencia/experience


def _near_experience(text: str, m: re.Match) -> bool:
    """"5 años de experiencia", "experience: 3 years": la cifra va pegada a la palabra experiencia.
    Una cifra suelta a 80 caracteres ("1 year" de un nivel, "6 years" de otro) no cuenta."""
    return ("experien" in text[m.end():m.end() + EXPERIENCE_NEAR]
            or "experien" in text[max(0, m.start() - EXPERIENCE_NEAR):m.start()])


def _extract_requirements(job) -> dict:
    """Requisitos y bloqueadores de la oferta, extraídos con regex (sin IA).

    `job` puede ser un Job o un dict con los mismos campos. Devuelve un dict serializable:

        anos_min / anos_max   años de experiencia pedidos en la primera mención junto a "experiencia" (mín. y máx. del rango), o None
        multinivel            aviso que abre a junior/mid/senior a la vez ("Junior 0–2 years …")
        entry                 dice "entry-level", "early career", "new grad" o "recién egresado"
        ingles                "alto" (B2+/fluido), "intermedio" (B1–B2, "good English") o None
        modalidad             "remoto" | "híbrido" | "presencial" | None
        pais_restringido      "Filipinas", "EE. UU."… si exige residir/trabajar en otro país; None si no
        horario               "nocturno" | "horario EE. UU." | "horario Europa" | "horario Asia" | None
        salario               {"min", "max", "moneda", "fuente": "portal"|"texto"} o None
        anotacion             trabajo de anotación/idiomas para IA
        alertas_estafa        lista de señales ("pide pagar por el proceso"…), ignorando negaciones
        bloqueadores          frases cortas en español (ver `blockers`)

    Cómo mostrarlo en el panel (main.py / templates, sin columnas nuevas): calcularlo al vuelo
    en la vista, p. ej. `req = extract_requirements(job)`, y pintar `req["bloqueadores"]` como
    chips rojos ("inglés alto (CV A2)", "solo residentes de Filipinas"), y `anos_min`,
    `modalidad`, `horario` y `salario` como chips neutros junto al título. La nota de IA trae
    además sus propios `blockers` en `score_data`; `rank_value` usa ambos.
    """
    get = _getter(job)
    title = _norm(get("title"))
    body = _norm(get("description"))[:6000]
    text = f"{title}\n{body}"

    lows, highs = [], []
    body_y = NUM_WORDS.sub(lambda m: _WORDS[m.group(0)], body)
    for m in YEARS.finditer(body_y):
        lo = int(m.group(1))
        if 0 < lo < 20 and _near_experience(body_y, m):
            lows.append(lo)
            highs.append(int(m.group(2)) if m.group(2) else lo)
    multinivel = bool(re.search(r"junior\W{0,5}\(?\s*0\s*(?:-|–|a|to)\s*[12]\s*(?:anos?|years?)", body))
    entry = bool(MULTI_JR.search(body))
    if not lows and MONTHS.search(body):
        lows, highs = [0], [0]  # "6 meses de experiencia": menos de un año
    if lows:
        # La primera mención pegada a "experiencia" es el requisito general; las siguientes suelen ser
        # otros niveles ("Senior track: 6 years") o deseables. Medido contra radar.db: mejor que el máximo.
        anos_min, anos_max = lows[0], highs[0]
    else:
        anos_min = anos_max = None

    eng_text = B1_B2.sub("b1", text)
    ingles = "alto" if ENGLISH_HIGH.search(eng_text) else ("intermedio" if ENGLISH_MID.search(text) else None)

    modality = _norm(get("remote_modality"))
    if "hybrid" in modality:
        modalidad = "híbrido"
    elif modality in ("no_remote", "presencial", "onsite", "on_site"):
        modalidad = "presencial"
    elif "remote" in modality:
        modalidad = "remoto"
    elif HYBRID.search(text):
        modalidad = "híbrido"
    elif ONSITE.search(text):
        modalidad = "presencial"
    elif REMOTE.search(text):
        modalidad = "remoto"
    else:
        modalidad = None

    if NIGHT.search(text):
        horario = "nocturno"
    elif TZ_ASIA.search(body):
        horario = "horario Asia"
    elif TZ_EU.search(body):
        horario = "horario Europa"
    elif TZ_US.search(body):
        horario = "horario EE. UU."
    else:
        horario = None

    req = {
        "anos_min": anos_min,
        "anos_max": anos_max,
        "multinivel": multinivel,
        "entry": entry,
        "ingles": ingles,
        "modalidad": modalidad,
        "pais_restringido": _country_restriction(body),
        "horario": horario,
        "salario": _salary(get, body),
        "anotacion": bool(GIG.search(title) or (GIG.search(body) and "freelance" in title)),
        "alertas_estafa": _scam_signals(text),
        "exige_estudiante": bool(STUDENT_REQ.search(body)),
    }
    req["bloqueadores"] = blockers(req)
    return req


def blockers(req: dict) -> list[str]:
    """Bloqueadores duros para un perfil junior con inglés A2 que vive en Colombia."""
    out = []
    if req.get("pais_restringido"):
        out.append(f"solo para {req['pais_restringido']}")
    if req.get("ingles") == "alto" and _english_rank() < 4:
        out.append(f"inglés alto (CV {getattr(config, 'ENGLISH_LEVEL', 'A2')})")
    if (req.get("anos_min") or 0) >= 3 and not req.get("multinivel"):
        out.append(f"pide {req['anos_min']}+ años")
    if req.get("anotacion"):
        out.append("no es desarrollo (anotación/idiomas)")
    if req.get("exige_estudiante") and not getattr(config, "IS_STUDENT", False):
        out.append("exige ser estudiante")
    if req.get("horario") == "nocturno":
        out.append("horario nocturno")
    out += [f"posible estafa: {s}" for s in req.get("alertas_estafa") or []]
    return out


def fit(job) -> tuple[int, list[str]]:
    """Devuelve (nota 0–100, razones). `job` puede ser un Job o un dict con los mismos campos."""
    global _SKILL_RX
    if _SKILL_RX is None:
        _SKILL_RX = _skills()
    get = _getter(job)
    title = _norm(get("title"))
    body = _norm(get("description"))[:6000]
    seniority = _norm(get("seniority"))
    text = f"{title}\n{body}"
    req = extract_requirements(job)
    score, why = 35, []

    def add(points: int, reason: str):
        nonlocal score
        score += points
        why.append(f"{'+' if points > 0 else ''}{points} {reason}")

    # 1. Nivel: el título manda (p. ej. "Junior/Semi Senior" cuenta como junior)
    clean = REQ_CODE.sub(" ", title)          # "JR-186652" es un código, no "junior"
    senior_t = SENIOR.search(SEMI.sub(" ", clean))
    junior_t = JUNIOR.search(clean)
    level = None
    if junior_t and senior_t:                  # "Junior a Senior": rango amplio
        add(0, "nivel mixto")
        level = "mixto"
    elif junior_t or (not senior_t and seniority in ("junior", "sin experiencia")):
        add(+25, "nivel junior")
        level = "junior"
    elif senior_t or seniority in ("senior", "expert"):
        add(-35, "nivel senior")
        level = "senior"
    elif SEMI.search(clean) or seniority == "semi senior":
        add(-5, "semi senior")
    # v2: avisos multinivel o entry-level cuentan como junior; "associate" un poco menos
    if level is None or (level == "senior" and not senior_t):
        if req["multinivel"] or req["entry"]:
            add(+20, "aviso abierto a junior/entry-level")
            level = "junior"
        elif ASSOCIATE.search(clean):
            add(+10, "nivel associate")
            level = "junior"
    elif level == "senior" and req["multinivel"]:
        add(+20, "aviso multinivel (incluye junior)")

    # 2. Años de experiencia pedidos (v2: los rangos cuentan por el mínimo)
    need = req["anos_min"]
    if need is not None and req["multinivel"]:
        pass  # "Junior 0–2 / Mid 2–5 / Senior 5+": no penaliza
    elif need is not None:
        if need >= 5:
            add(-30, f"pide {need}+ años")
        elif need >= 3:
            add(-20 if level is None else -15, f"pide {need}+ años")
        elif need == 2:
            add(-5, "pide 2 años")
        elif need <= 1:
            add(+10, f"pide {need} año{'s' if need != 1 else ''}")

    # 3. Tipo de rol
    if req["anotacion"]:
        add(-40, "trabajo de anotación/idiomas para IA")
    elif CORE_DEV.search(title):
        add(+15, "rol de desarrollo")
    elif NON_TECH.search(title):
        add(-40, "rol no técnico")
    elif NON_TECH_EXTRA.search(title):
        add(-25, "rol no técnico (asistente/coordinación)")
    elif TECH_ROLE.search(title):
        add(+5, "rol técnico")
    else:
        add(-15, "rol poco claro")

    # 4. Habilidades de tu CV que aparecen en la oferta
    hits = [name for name, rx in _SKILL_RX if rx.search(text)]
    if hits:
        add(min(15, 3 * len(hits)), "usa " + ", ".join(hits[:5]))

    # 5. Idioma (el CV dice inglés A2: config.ENGLISH_LEVEL)
    level_en = _english_rank()
    if req["ingles"] == "alto" and level_en < 4:
        add(-20, f"pide inglés alto (CV {config.ENGLISH_LEVEL})")
    elif req["ingles"] == "intermedio" and level_en < 3:
        add(-8, "pide inglés intermedio")
    elif req["ingles"] is None and ENGLISH_LOW.search(text):
        add(+5, "inglés deseable/básico")
    if len(SPANISH_POST.findall(body)) >= 2:
        add(+5, "oferta en español")

    # 6. Ubicación
    if req["pais_restringido"]:
        add(-35, f"restringida a {req['pais_restringido']}")
    if LOCAL.search(text):
        add(+10, "Medellín/Antioquia")
    elif "colombia" in text or "Colombia" in (get("countries") or []):
        add(+5, "menciona Colombia")

    # 7. Prácticas que exigen ser estudiante (config.IS_STUDENT)
    if req["exige_estudiante"] and not config.IS_STUDENT:
        add(-20, "práctica que exige ser estudiante")

    # 8. Señales de estafa (con manejo de negación)
    if req["alertas_estafa"]:
        add(-50, "posible estafa: " + ", ".join(req["alertas_estafa"]))

    return max(0, min(100, score)), why


def _as_utc(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    if isinstance(d, str):
        try:
            d = datetime.fromisoformat(d)
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def rank_value(job, now: datetime | None = None) -> float:
    """Valor para ordenar el panel ("mejor primero"). Función pura: no toca la BD.

    - Base: con nota de IA, `fit_pct` (0–100) si la IA lo dio, o 20·score − 10; sin IA, el
      prefiltro con descuento (0,8·fit), para que una oferta puntuada buena gane a una sin puntuar.
    - Bloqueadores: −15 por cada uno (máx. 3), tomando el mayor entre los de la IA
      (`score_data["blockers"]`) y los de `extract_requirements`, para no contar dos veces lo mismo.
    - Antigüedad: sin descuento las primeras 2 semanas; después, vida media de 30 días.
    """
    get = _getter(job)
    data = get("score_data") or {}
    score = get("score")
    if score:
        base = float(data.get("fit_pct") or 0) or 20.0 * score - 10
    else:
        base = 0.8 * (get("fit") or 0)
    local = extract_requirements(job)["bloqueadores"]
    hard = min(3, max(len(local), len(data.get("blockers") or [])))
    now = _as_utc(now) or datetime.now(timezone.utc)
    when = _as_utc(get("published_at")) or _as_utc(get("first_seen_at"))
    age = (now - when).total_seconds() / 86400 if when else 0.0
    decay = math.pow(0.5, max(0.0, age - config.RANK_FRESH_DAYS) / config.RANK_HALF_LIFE_DAYS)
    return round(max(0.0, base - 15 * hard) * decay, 3)
