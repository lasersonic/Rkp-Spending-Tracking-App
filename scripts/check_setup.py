"""Pre-flight check: are LINE, iApp, Google Sheets and Drive set up correctly?

Run it with the same environment variables as the bot (see README → Secrets):

    python -m scripts.check_setup
    python -m scripts.check_setup --service-url https://expense-bot-xxxx.a.run.app
    python -m scripts.check_setup --ocr-sample receipt.jpg   # one real OCR call, ~1.34 ฿

Nothing is changed except a tiny test file that is created in the Drive
folder and deleted straight away.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import Settings, get_settings
from app.storage import TABS

OK, WARN, FAIL = "✅", "⚠️ ", "❌"
LINE_USER_ID = re.compile(r"^U[0-9a-f]{32}$")


@dataclass
class Result:
    status: str
    name: str
    detail: str = ""


def check_config(s: Settings) -> list[Result]:
    out = []
    required = {
        "LINE_CHANNEL_SECRET": s.line_channel_secret,
        "LINE_CHANNEL_ACCESS_TOKEN": s.line_channel_access_token,
        "IAPP_API_KEY": s.iapp_api_key,
        "GOOGLE_SA_JSON": s.google_sa_info,
        "SHEET_ID": s.sheet_id,
        "DRIVE_FOLDER_ID": s.drive_folder_id,
        "ALLOWED_USER_IDS": s.allowed_user_ids,
    }
    missing = [k for k, v in required.items() if not v]
    out.append(Result(FAIL if missing else OK, "Required settings", "missing: " + ", ".join(missing) if missing else "all present"))

    if s.google_sa_info and s.google_sa_info.get("type") != "service_account":
        out.append(Result(FAIL, "GOOGLE_SA_JSON", "not a service account key file"))
    if s.google_drive_token_info is None:
        out.append(Result(WARN, "GOOGLE_DRIVE_TOKEN_JSON", "not set; Drive uploads use the service account, which only works with a Shared Drive"))
    elif not s.google_drive_token_info.get("refresh_token"):
        out.append(Result(FAIL, "GOOGLE_DRIVE_TOKEN_JSON", "has no refresh_token; rerun `scripts.setup drive-token`"))

    bad = [u for u in s.allowed_user_ids if not LINE_USER_ID.match(u)]
    if bad:
        out.append(Result(FAIL, "ALLOWED_USER_IDS", f"not LINE user IDs (U + 32 hex chars): {', '.join(bad)}"))
    if s.tz_name != "Asia/Bangkok":
        out.append(Result(WARN, "TZ", f"is {s.tz_name}, expected Asia/Bangkok"))
    return out


def check_line(s: Settings) -> list[Result]:
    from linebot.v3.messaging import ApiClient, ApiException, Configuration, MessagingApi

    out = []
    try:
        with ApiClient(Configuration(access_token=s.line_channel_access_token)) as api:
            line = MessagingApi(api)
            info = line.get_bot_info()
            out.append(Result(OK, "LINE access token", f"bot “{info.display_name}” ({info.basic_id})"))
            if info.chat_mode != "bot":
                out.append(Result(WARN, "LINE response mode", f"is “{info.chat_mode}”; set it to Bot in LINE Official Account Manager"))
            hook = line.get_webhook_endpoint()
            if not hook.endpoint.endswith("/callback"):
                out.append(Result(FAIL, "LINE webhook URL", f"{hook.endpoint or '(not set)'}; should be <service url>/callback"))
            elif not hook.active:
                out.append(Result(FAIL, "LINE webhook", f"{hook.endpoint} is set but “Use webhook” is off"))
            else:
                test = line.test_webhook_endpoint()
                ok = bool(test.success)
                out.append(Result(OK if ok else FAIL, "LINE webhook test", f"{hook.endpoint} → HTTP {test.status_code} {test.reason or ''}".strip()))
    except ApiException as exc:
        hint = "token invalid or revoked; issue a new one in LINE Developers" if exc.status == 401 else f"HTTP {exc.status}"
        out.append(Result(FAIL, "LINE API", hint))
    except Exception as exc:
        out.append(Result(FAIL, "LINE API", f"{type(exc).__name__}: {exc}"))
    return out


def check_iapp(s: Settings, sample: Path | None) -> list[Result]:
    from app.iapp import IappClient, IappAuthError, IappError, parse_response
    from app.pipeline import detect_type

    if sample is None:
        # An empty upload can't be OCR'd, so it shouldn't use credits; a wrong
        # key should still be refused with 401/403.
        try:
            resp = httpx.post(s.iapp_url, headers={"apikey": s.iapp_api_key},
                              files={"file": ("empty.jpg", b"", "image/jpeg")}, timeout=30)
        except httpx.HTTPError as exc:
            return [Result(FAIL, "iApp API", f"can't reach {s.iapp_url}: {type(exc).__name__}")]
        if resp.status_code in (401, 403):
            return [Result(FAIL, "iApp API key", f"rejected (HTTP {resp.status_code})")]
        if resp.status_code == 402:
            return [Result(FAIL, "iApp credits", "out of credits (HTTP 402)")]
        return [Result(OK, "iApp API key", f"accepted (HTTP {resp.status_code} for an empty file); use --ocr-sample for a full test")]

    content = sample.read_bytes()
    kind = detect_type(content)
    if not kind:
        return [Result(FAIL, "iApp OCR", f"{sample} is not a JPG/PNG/HEIC/PDF")]
    try:
        r = parse_response(IappClient(s.iapp_api_key, s.iapp_url).ocr(content, sample.name, kind[0]))
    except IappAuthError as exc:
        return [Result(FAIL, "iApp OCR", str(exc))]
    except IappError as exc:
        return [Result(FAIL, "iApp OCR", str(exc))]
    found = f"{r.vendor_name or '?'} · {r.grand_total} ฿ · {r.receipt_date} · {len(r.items)} items · conf {r.min_confidence}"
    status = OK if r.grand_total is not None else WARN
    return [Result(status, "iApp OCR", found + ("" if status == OK else " (no grand total parsed; share the raw JSON)"))]


def _google_error(exc: Exception) -> str:
    """Plain-language hint for credential failures."""
    text = str(exc)
    if "invalid_grant" in text:
        return "Google rejected the credentials (key revoked, account deleted, or token expired); create a new key/token"
    if "invalid_client" in text:
        return "OAuth client not found; check the client ID/secret in GOOGLE_DRIVE_TOKEN_JSON"
    return f"{type(exc).__name__}: {text[:200]}"


def check_sheets(s: Settings) -> list[Result]:
    import gspread

    from app.storage import SHEETS_SCOPES

    try:
        ss = gspread.service_account_from_dict(s.google_sa_info, scopes=SHEETS_SCOPES).open_by_key(s.sheet_id)
    except gspread.exceptions.SpreadsheetNotFound:
        email = (s.google_sa_info or {}).get("client_email", "the service account")
        return [Result(FAIL, "Google Sheet", f"not found or not shared; share it (Editor) with {email}")]
    except Exception as exc:
        return [Result(FAIL, "Google Sheet", _google_error(exc))]
    out = [Result(OK, "Google Sheet", f"opened “{ss.title}”")]
    tabs = {w.title: w for w in ss.worksheets()}
    missing = [t for t in [*TABS, "Dashboard"] if t not in tabs]
    wrong = [t for t, cols in TABS.items() if t in tabs and tabs[t].row_values(1) != cols]
    if missing or wrong:
        detail = "; ".join(filter(None, [
            f"missing tabs: {', '.join(missing)}" if missing else "",
            f"unexpected headers: {', '.join(wrong)}" if wrong else "",
        ]))
        out.append(Result(WARN if not wrong else FAIL, "Sheet tabs", detail + " (run `scripts.setup init-sheet`; the bot also creates missing tabs)"))
    else:
        out.append(Result(OK, "Sheet tabs", "all tabs and headers in place"))
    return out


def check_drive(s: Settings) -> list[Result]:
    from googleapiclient.errors import HttpError

    from app.storage import DriveStore

    try:
        drive = DriveStore.from_settings(s.google_sa_info, s.google_drive_token_info, s.drive_folder_id)
        meta = drive.svc.files().get(fileId=s.drive_folder_id, fields="name,mimeType,trashed", supportsAllDrives=True).execute()
    except HttpError as exc:
        hint = ("not visible to the bot; with the drive.file scope the folder must be created by "
                "`scripts.setup create-folder`") if exc.resp.status == 404 else f"HTTP {exc.resp.status}"
        return [Result(FAIL, "Drive folder", hint)]
    except Exception as exc:
        return [Result(FAIL, "Drive", _google_error(exc))]
    if meta.get("mimeType") != "application/vnd.google-apps.folder" or meta.get("trashed"):
        return [Result(FAIL, "Drive folder", f"“{meta.get('name')}” is not an active folder")]
    out = [Result(OK, "Drive folder", f"“{meta['name']}”")]
    try:
        from googleapiclient.http import MediaInMemoryUpload

        f = drive.svc.files().create(
            body={"name": "_setup_check.txt", "parents": [s.drive_folder_id]},
            media_body=MediaInMemoryUpload(b"ok", mimetype="text/plain"), fields="id", supportsAllDrives=True,
        ).execute()
        drive.svc.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
        out.append(Result(OK, "Drive upload", "test file written and deleted"))
    except HttpError as exc:
        text = str(exc)
        hint = ("service accounts have no storage on personal Google accounts; set GOOGLE_DRIVE_TOKEN_JSON"
                if "storageQuotaExceeded" in text or "storage quota" in text.lower() else f"HTTP {exc.resp.status}")
        out.append(Result(FAIL, "Drive upload", hint))
    return out


def check_service(url: str) -> list[Result]:
    url = url.rstrip("/")
    try:
        health = httpx.get(url + "/", timeout=60)
        unsigned = httpx.post(url + "/callback", content=b'{"events":[]}', timeout=60)
    except httpx.HTTPError as exc:
        return [Result(FAIL, "Cloud Run service", f"can't reach {url}: {type(exc).__name__}")]
    out = [Result(OK if health.status_code == 200 else FAIL, "Cloud Run health", f"HTTP {health.status_code}")]
    out.append(Result(OK if unsigned.status_code == 400 else FAIL, "Signature check",
                      "unsigned requests are rejected" if unsigned.status_code == 400 else f"unsigned request got HTTP {unsigned.status_code}"))
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--service-url", help="deployed Cloud Run URL, to check the webhook itself")
    p.add_argument("--ocr-sample", type=Path, help="run one real receipt through iApp (costs ~1.34 ฿)")
    p.add_argument("--skip", nargs="*", default=[], choices=["line", "iapp", "sheets", "drive"])
    args = p.parse_args(argv)

    s = get_settings()
    results = check_config(s)
    if "line" not in args.skip and s.line_channel_access_token:
        results += check_line(s)
    if "iapp" not in args.skip and s.iapp_api_key:
        results += check_iapp(s, args.ocr_sample)
    if "sheets" not in args.skip and s.google_sa_info and s.sheet_id:
        results += check_sheets(s)
    if "drive" not in args.skip and s.drive_folder_id and (s.google_sa_info or s.google_drive_token_info):
        results += check_drive(s)
    if args.service_url:
        results += check_service(args.service_url)

    for r in results:
        print(f"{r.status} {r.name}: {r.detail}")
    failed = sum(r.status == FAIL for r in results)
    print(f"\n{'All good.' if not failed else f'{failed} problem(s) to fix.'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
