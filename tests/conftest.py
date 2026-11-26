from __future__ import annotations

import pytest

from ap_automation import db
from ap_automation.config import Settings
from ap_automation.datagen import generate_world, load_erp
from ap_automation.invoice_pdfs import write_invoices
from ap_automation.pipeline import Pipeline, build_components, load_arrival_times, payment_run, set_as_of


def make_settings(tmp_path, n_invoices: int = 70) -> Settings:
    s = Settings()
    s.db_path = tmp_path / "ap.db"
    s.inbox_dir = tmp_path / "inbox"
    s.output_dir = tmp_path / "outputs"
    s.n_invoices = n_invoices
    s.seed = 7
    s.extractor = "offline"
    s.drive_mode = s.jira_mode = s.sheets_mode = "mock"
    s.ensure_dirs()
    return s


def setup_world(s: Settings):
    conn = db.connect(s.db_path)
    db.init_schema(conn)
    world = generate_world(s.seed, s.n_invoices, s.as_of)
    load_erp(conn, world)
    set_as_of(conn, s.as_of.isoformat())
    write_invoices(world.invoices, s.inbox_dir, s.manifest_path, s.seed)
    return conn, world


def run_pipeline(s: Settings, conn):
    extractor, source, jira, sheet = build_components(s, conn, "mock", load_arrival_times(s))
    summary = Pipeline(s, conn, extractor, source, jira, sheet).run()
    payment_run(conn, s.as_of.isoformat())
    return summary, jira


@pytest.fixture(scope="session")
def processed(tmp_path_factory):
    """One full offline run shared by the read-only tests."""
    tmp = tmp_path_factory.mktemp("run")
    s = make_settings(tmp)
    conn, world = setup_world(s)
    summary, jira = run_pipeline(s, conn)
    return {"settings": s, "conn": conn, "world": world, "summary": summary, "jira": jira}


@pytest.fixture(scope="session")
def world_pdfs(processed):
    return processed["world"], processed["settings"].inbox_dir
