"""M0 helper: run receipts through iApp and show what the bot would log.

    IAPP_API_KEY=... python -m scripts.try_ocr receipt1.jpg receipt2.pdf

Each file costs about 1.34 ฿ per page. The raw responses are saved next to
each file as <name>.iapp.json; share one so the parser can be checked
against iApp's real output.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

from app.config import get_settings
from app.iapp import IappClient, parse_response
from app.normalize import validate
from app.pipeline import detect_type


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    s = get_settings()
    client = IappClient(s.iapp_api_key, s.iapp_url)
    for name in argv:
        path = Path(name)
        content = path.read_bytes()
        kind = detect_type(content)
        if not kind:
            print(f"{name}: not a JPG/PNG/PDF, skipped")
            continue
        raw = client.ocr(content, path.name, kind[0])
        path.with_suffix(path.suffix + ".iapp.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        r = parse_response(raw, date.today())
        print(f"\n=== {name}")
        for field in ("receipt_date", "vendor_name", "vendor_tax_id", "invoice_no", "subtotal",
                      "discount", "after_discount", "vat", "grand_total", "net_before_vat", "min_confidence"):
            print(f"  {field:16} {getattr(r, field)}")
        for i in r.items:
            print(f"  item {i.item_no:>2}: {i.item_name} × {i.qty} @ {i.unit_cost} = {i.total_cost}")
        problems = validate(r, date.today())
        print("  review:", "; ".join(problems) if problems else "OK ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
