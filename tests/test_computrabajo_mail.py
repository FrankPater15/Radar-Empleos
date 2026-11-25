"""Lector de Perfect Match de Computrabajo (estructura real, datos inventados)."""
from radar.sources import computrabajo_mail as ct

GO = ("https://go.computrabajo.com/go/app/o_detail?url=https%3A%2F%2Fco.computrabajo.com%2Fofertas-de-trabajo"
      "%2Foferta-de-trabajo-de-dev-en-medellin-ABCDEF0123456789ABCDEF%3Futm_source%3Dx&idu=SECRETO")
MATCH = f"""<style>p {{}}</style><p>Perfect Match</p><p>de la semana</p><p>Dev Junior</p><p>4.8</p>
<p>Acme</p><p>Antioquia</p><p>$ 3.000.000,00 (Mensual)</p><p>Presencial y remoto</p>
<a href="{GO}">Postúlate</a><p>Descripción de la oferta</p><p>Extracto...</p><a href="{GO}">Ver oferta</a>"""


def test_perfect_match():
    [j] = ct.parse_alert(MATCH)
    assert (j["id"], j["title"], j["company"]) == ("computrabajo:ABCDEF0123456789ABCDEF", "Dev Junior", "Acme")
    assert j["url"] == ("https://co.computrabajo.com/ofertas-de-trabajo/"
                        "oferta-de-trabajo-de-dev-en-medellin-ABCDEF0123456789ABCDEF")
    assert j["remote_modality"] == "hybrid" and j["countries"] == ["Antioquia", "Colombia"]
    assert "3.000.000" in j["description"] and "Extracto" in j["description"] and j["min_salary"] is None


def test_without_rating_promos_and_foreign_links():
    assert ct.parse_alert(MATCH.replace("<p>4.8</p>", ""))[0]["company"] == "Acme"
    assert ct.parse_alert('<p>Hola</p><a href="https://co.computrabajo.com/ofertas-de-trabajo/?q=dev">Ver</a>') == []
    assert ct.parse_alert(MATCH.replace("co.computrabajo.com", "malo.com")) == []
