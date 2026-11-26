"""Runtime settings.

Everything has a sensible offline default so `python -m ap_automation demo`
runs with no credentials. Each setting can be overridden with an environment
variable (see README > Configuration).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    # Storage
    db_path: Path = field(default_factory=lambda: Path(_env("AP_DB_PATH", str(ROOT / "data" / "ap.db"))))
    inbox_dir: Path = field(default_factory=lambda: Path(_env("AP_INBOX_DIR", str(ROOT / "data" / "inbox"))))
    output_dir: Path = field(default_factory=lambda: Path(_env("AP_OUTPUT_DIR", str(ROOT / "outputs"))))

    # Synthetic data
    seed: int = field(default_factory=lambda: int(_env("AP_SEED", "42")))
    n_invoices: int = field(default_factory=lambda: int(_env("AP_N_INVOICES", "150")))
    # The "today" of the demo world. Fixed so results are reproducible.
    as_of: date = field(default_factory=lambda: date.fromisoformat(_env("AP_AS_OF", "2026-11-20")))

    # Business rules
    amount_tolerance_pct: float = field(default_factory=lambda: float(_env("AP_TOLERANCE_PCT", "0.02")))
    high_dollar_threshold: float = field(default_factory=lambda: float(_env("AP_HIGH_DOLLAR", "10000")))
    sla_days: int = field(default_factory=lambda: int(_env("AP_SLA_DAYS", "3")))

    # Extraction: "offline" (deterministic rule-based runnable) or "llm" (Claude via LangChain)
    extractor: str = field(default_factory=lambda: _env("AP_EXTRACTOR", "offline"))
    llm_model: str = field(default_factory=lambda: _env("AP_LLM_MODEL", "claude-opus-5-5"))

    # Integrations: "mock" (local files / SQLite) or "live" (real APIs)
    drive_mode: str = field(default_factory=lambda: _env("AP_DRIVE_MODE", "mock"))
    jira_mode: str = field(default_factory=lambda: _env("AP_JIRA_MODE", "mock"))
    sheets_mode: str = field(default_factory=lambda: _env("AP_SHEETS_MODE", "mock"))

    # Live integration credentials (only read when a mode is "live")
    jira_base_url: str = field(default_factory=lambda: _env("JIRA_BASE_URL", ""))
    jira_email: str = field(default_factory=lambda: _env("JIRA_EMAIL", ""))
    jira_api_token: str = field(default_factory=lambda: _env("JIRA_API_TOKEN", ""))
    jira_project_key: str = field(default_factory=lambda: _env("JIRA_PROJECT_KEY", "AP"))
    google_service_account_file: str = field(default_factory=lambda: _env("GOOGLE_SERVICE_ACCOUNT_FILE", ""))
    drive_folder_id: str = field(default_factory=lambda: _env("DRIVE_INBOX_FOLDER_ID", ""))
    sheet_id: str = field(default_factory=lambda: _env("APPROVED_SHEET_ID", ""))
    sheet_tab: str = field(default_factory=lambda: _env("APPROVED_SHEET_TAB", "Approved for payment"))

    @property
    def invoices_dir(self) -> Path:
        return self.inbox_dir

    @property
    def manifest_path(self) -> Path:
        return self.inbox_dir.parent / "invoice_manifest.json"

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)


BILL_TO = {
    "name": "Bluefin Mobility, Inc.",
    "address": ["Accounts Payable", "500 Harbor Street, Suite 1200", "San Francisco, CA 94105"],
}
