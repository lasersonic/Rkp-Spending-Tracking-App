"""One-time import of the Telegram receipt archive (M4).

1. Telegram Desktop -> chat -> ⋮ -> Export chat history: tick Photos and Files,
   format "Machine-readable JSON". You get a folder with result.json.
2. Estimate the cost first:
       python -m scripts.backfill import ./ChatExport_2026-10-17 --dry-run
3. Import (uses the same env vars as the bot):
       python -m scripts.backfill import ./ChatExport_2026-10-17
4. Unknown vendors are added to the Vendors tab with an empty category.
   Fill in the category column there, then run:
       python -m scripts.backfill apply-categories
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from app.categorize import Vendor, find_vendor, vendor_key
from app.config import get_settings
from app.iapp import IappClient
from app.pipeline import Bot, detect_type, pdf_page_count
from app.storage import DriveStore, SheetStore

COST_PER_PAGE = 1.34  # ฿, 1.25 + VAT


class _QuietLine:
    """No chat to reply to during a backfill; owner alerts go to stdout."""

    def send(self, to, msg, reply_token=None):
        print(f"  [owner alert] {msg.text}")

    def show_loading(self, *a, **k):
        pass


def telegram_files(export_dir: Path):
    data = json.loads((export_dir / "result.json").read_text(encoding="utf-8"))
    for m in data.get("messages", []):
        if m.get("type") != "message":
            continue
        rel = m.get("photo") or m.get("file")
        if not rel or not isinstance(rel, str) or rel.startswith("(File not included"):
            continue
        path = export_dir / rel
        if path.is_file():
            yield path, datetime.fromisoformat(m["date"]), str(m.get("from") or m.get("from_id") or "")


def make_bot() -> Bot:
    s = get_settings()
    sheets = SheetStore.from_service_account(s.google_sa_info, s.sheet_id)
    sheets.ensure_schema()
    return Bot(
        settings=s,
        line=_QuietLine(),
        sheets=sheets,
        drive=DriveStore.from_settings(s.google_sa_info, s.google_drive_token_info, s.drive_folder_id),
        ocr=IappClient(s.iapp_api_key, s.iapp_url),
    )


def cmd_import(args) -> int:
    files = list(telegram_files(Path(args.export_dir)))
    if args.limit:
        files = files[: args.limit]
    pages = 0
    for path, _, _ in files:
        content = path.read_bytes()
        kind = detect_type(content)
        if kind:
            pages += pdf_page_count(content) if kind[1] == "pdf" else 1
    print(f"{len(files)} files, ~{pages} pages, est. OCR cost ~{pages * COST_PER_PAGE:.0f} ฿")
    if args.dry_run:
        return 0

    bot = make_bot()
    counts: dict[str, int] = {}
    for n, (path, sent_at, sender) in enumerate(files, start=1):
        outcome = bot.process_file(
            path.read_bytes(), path.name, submitted_by=f"telegram:{sender}",
            source="telegram_backfill", received_at=sent_at,
        )
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
        print(f"[{n}/{len(files)}] {path.name}: {outcome.status} {outcome.message.text.splitlines()[0]}")
        if outcome.needs_category:
            _remember_unknown_vendor(bot, outcome.receipt_id)
    print("Done:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print("Fill in empty categories on the Vendors tab, then run: python -m scripts.backfill apply-categories")
    return 0


def _remember_unknown_vendor(bot: Bot, receipt_id: str) -> None:
    r = bot.sheets.find_receipt(receipt_id)
    # The Receipts tab has no tax IDs, so new vendors are keyed by name; the
    # bot fills in the tax ID the next time a receipt from them shows one.
    if not r or not r["vendor_name"]:
        return
    if find_vendor(bot.sheets.vendors(), "", r["vendor_name"]) is None:
        bot.sheets.save_vendor(
            Vendor(vendor_key("", r["vendor_name"]), "", r["vendor_name"],
                   first_seen=bot.today().isoformat(), receipt_count=1)
        )


def cmd_apply_categories(args) -> int:
    bot = make_bot()
    vendors = bot.sheets.vendors()
    updated = 0
    for r in bot.sheets.receipts():
        if r["category"]:
            continue
        v = find_vendor(vendors, "", r["vendor_name"])
        if v and v.category:
            bot.sheets.update_receipt(r["receipt_id"], {"category": v.category})
            bot.sheets.set_items_category(r["receipt_id"], v.category)
            updated += 1
    print(f"Categorized {updated} receipts.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="import a Telegram Desktop export")
    imp.add_argument("export_dir")
    imp.add_argument("--dry-run", action="store_true", help="count files and estimate cost only")
    imp.add_argument("--limit", type=int, default=0, help="only the first N files (for a trial run)")
    imp.set_defaults(fn=cmd_import)
    sub.add_parser("apply-categories", help="fill receipt categories from the Vendors tab").set_defaults(
        fn=cmd_apply_categories
    )
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
