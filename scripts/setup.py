"""One-time setup helpers (M1). Run on your own computer.

    # 1. Get a Drive token for your Google account (opens a browser):
    python -m scripts.setup drive-token --client-secrets client_secret.json > drive_token.json

    # 2. Create the Receipts folder in your Drive; prints DRIVE_FOLDER_ID:
    GOOGLE_DRIVE_TOKEN_JSON=drive_token.json python -m scripts.setup create-folder

    # 3. Create the tabs, headers, categories and dashboard in the Sheet:
    GOOGLE_SA_JSON=sa.json SHEET_ID=... python -m scripts.setup init-sheet

Why a Drive token: a service account has no Drive storage on a personal
Google account, so uploads into your My Drive fail. The bot uploads as you
instead, with the narrow drive.file scope (it can only see files it created).
"""

from __future__ import annotations

import argparse
import json
import sys

from app.config import get_settings
from app.storage import DRIVE_SCOPES, DriveStore, SheetStore


def drive_token(args) -> int:
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets, scopes=DRIVE_SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    print(creds.to_json())
    print("Store this JSON in Secret Manager as GOOGLE_DRIVE_TOKEN_JSON.", file=sys.stderr)
    return 0


def create_folder(args) -> int:
    s = get_settings()
    drive = DriveStore.from_settings(s.google_sa_info, s.google_drive_token_info, "root")
    meta = {"name": args.name, "mimeType": "application/vnd.google-apps.folder"}
    folder = drive.svc.files().create(body=meta, fields="id,webViewLink").execute()
    print(json.dumps({"DRIVE_FOLDER_ID": folder["id"], "link": folder["webViewLink"]}, indent=2))
    return 0


def init_sheet(args) -> int:
    s = get_settings()
    SheetStore.from_service_account(s.google_sa_info, s.sheet_id).ensure_schema()
    print("Sheet ready: Receipts, LineItems, Vendors, Categories, Log, Dashboard.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("drive-token")
    t.add_argument("--client-secrets", required=True, help="OAuth client JSON (Desktop app type)")
    t.set_defaults(fn=drive_token)
    f = sub.add_parser("create-folder")
    f.add_argument("--name", default="Receipts")
    f.set_defaults(fn=create_folder)
    sub.add_parser("init-sheet").set_defaults(fn=init_sheet)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
