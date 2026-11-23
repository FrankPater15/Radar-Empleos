"""Endurecimiento: CSV injection, defensas del panel, URLs, ingesta robusta, índices, caché y scoring."""
import csv
import io
from datetime import datetime, timezone

from radar.models import Job
from radar.tracking import export_csv

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


# ---------- 1. CSV injection ----------
def test_export_csv_neutraliza_formulas():
    peligrosos = ['=HYPERLINK("http://evil","x")', "+cmd|' /C calc'!A0", "-2+3", "@SUM(A1)", "\tTAB", "\rCR"]
    jobs = [Job(id=f"a:{i}", source="a", title=t, company=t, url="https://x", status="encontrada",
                notes=t, first_seen_at=NOW) for i, t in enumerate(peligrosos)]
    buf = io.StringIO()
    export_csv(jobs, buf)
    rows = list(csv.reader(io.StringIO(buf.getvalue()), delimiter=";"))[1:]
    assert len(rows) == len(peligrosos)
    for row, original in zip(rows, peligrosos):
        for idx in (1, 2, 12):  # titulo, empresa, notas
            assert row[idx] == "'" + original
    # el texto normal no se toca
    ok = Job(id="a:ok", source="a", title="Analista QA", company="ACME", url="https://x", first_seen_at=NOW)
    buf = io.StringIO()
    export_csv([ok], buf)
    assert list(csv.reader(io.StringIO(buf.getvalue()), delimiter=";"))[1][1] == "Analista QA"
