"""Invoice intake: where new PDFs come from.

``LocalFolderSource`` reads a folder (the demo inbox, or a Google Drive
folder synced with Drive for desktop). ``GoogleDriveSource`` polls a Drive
folder through the Drive v3 API and downloads new PDFs to a local cache.
Both return ``InboxFile`` objects, so the pipeline does not care which one
is plugged in.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass
class InboxFile:
    name: str
    path: Path
    received_at: datetime


class InvoiceSource(Protocol):
    def list_files(self) -> list[InboxFile]: ...


class LocalFolderSource:
    def __init__(self, folder: Path, arrival_times: dict[str, str] | None = None):
        self.folder = Path(folder)
        self.arrival_times = arrival_times or {}

    def list_files(self) -> list[InboxFile]:
        out = []
        for p in sorted(self.folder.glob("*.pdf")):
            ts = self.arrival_times.get(p.name)
            received = datetime.fromisoformat(ts) if ts else datetime.fromtimestamp(p.stat().st_mtime)
            out.append(InboxFile(p.name, p, received))
        return sorted(out, key=lambda f: (f.received_at, f.name))


class GoogleDriveSource:
    """Polls a Drive folder. Needs `pip install -e .[google]` and a service account
    that the folder is shared with."""

    def __init__(self, folder_id: str, service_account_file: str, cache_dir: Path):
        if not folder_id or not service_account_file:
            raise ValueError("DRIVE_INBOX_FOLDER_ID and GOOGLE_SERVICE_ACCOUNT_FILE are required for live Drive mode")
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            service_account_file, scopes=["https://www.googleapis.com/auth/drive.readonly"])
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.folder_id = folder_id
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def list_files(self) -> list[InboxFile]:
        from googleapiclient.http import MediaIoBaseDownload

        q = f"'{self.folder_id}' in parents and mimeType = 'application/pdf' and trashed = false"
        files, token = [], None
        while True:
            resp = self.service.files().list(
                q=q, fields="nextPageToken, files(id, name, createdTime)", pageToken=token, pageSize=100,
            ).execute()
            files.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                break
        out = []
        for f in files:
            local = self.cache_dir / f"{f['id']}_{f['name']}"
            if not local.exists():
                buf = io.BytesIO()
                downloader = MediaIoBaseDownload(buf, self.service.files().get_media(fileId=f["id"]))
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                local.write_bytes(buf.getvalue())
            received = datetime.fromisoformat(f["createdTime"].replace("Z", "+00:00")).replace(tzinfo=None)
            out.append(InboxFile(f["name"], local, received))
        return sorted(out, key=lambda f: (f.received_at, f.name))
