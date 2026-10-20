"""Run receipts through Google Vision and show what the bot would log.

    python -m scripts.try_ocr receipt1.jpg receipt2.pdf
    python -m scripts.try_ocr receipt1.jpg --rows     # also show the text rows the parser sees

Uses GOOGLE_SA_JSON. Free for the first 1,000 pages a month. The raw responses
are saved next to each file as <name>.vision.json, so a misread field can be
traced back to what Vision returned.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from app.config import get_settings
from app.normalize import validate
from app.pipeline import detect_type
from app.vision import VisionClient, parse_response, rows_of


def main(argv: list[str]) -> int:
    show_rows = "--rows" in argv
    names = [a for a in argv if a != "--rows"]
    if not names:
        print(__doc__)
        return 1
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Thai text on a Windows console
    client = VisionClient(get_settings().google_sa_info)
    for name in names:
        path = Path(name)
        content = path.read_bytes()
        kind = detect_type(content)
        if not kind:
            print(f"{name}: not a JPG/PNG/PDF, skipped")
            continue
        raw = client.ocr(content, path.name, kind[0])
        path.with_suffix(path.suffix + ".vision.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"\n=== {name}")
        if show_rows:
            for row in rows_of(raw):
                print(f"  | {row.text}")
        r = parse_response(raw, date.today())
        for field in ("receipt_date", "vendor_name", "vendor_tax_id", "invoice_no", "subtotal",
                      "vat", "grand_total", "net_before_vat", "min_confidence"):
            print(f"  {field:16} {getattr(r, field)}")
        for i in r.items:
            print(f"  item {i.item_no:>2}: {i.item_name} × {i.qty} @ {i.unit_cost} = {i.total_cost}")
        problems = validate(r, date.today())
        print("  review:", "; ".join(problems) if problems else "OK ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
