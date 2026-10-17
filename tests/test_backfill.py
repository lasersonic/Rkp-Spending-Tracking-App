import json

from scripts import backfill
from tests.fakes import JPEG
from tests.test_pipeline import make_bot


def make_export(tmp_path):
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "a.jpg").write_bytes(JPEG)
    (tmp_path / "photos" / "b.jpg").write_bytes(JPEG)  # same file forwarded twice
    messages = [
        {"id": 1, "type": "message", "date": "2026-03-02T09:15:00", "from": "Owner", "photo": "photos/a.jpg"},
        {"id": 2, "type": "message", "date": "2026-03-03T10:00:00", "from": "Owner", "photo": "photos/b.jpg"},
        {"id": 3, "type": "message", "date": "2026-03-04T10:00:00", "from": "Owner", "text": "no file"},
        {"id": 4, "type": "message", "date": "2026-03-05T10:00:00", "from": "Owner",
         "file": "(File not included. Change data exporting settings to download.)"},
        {"id": 5, "type": "service", "date": "2026-03-05T10:00:00"},
    ]
    (tmp_path / "result.json").write_text(json.dumps({"messages": messages}), encoding="utf-8")


def test_reads_telegram_export(tmp_path):
    make_export(tmp_path)
    files = list(backfill.telegram_files(tmp_path))
    assert [p.name for p, _, _ in files] == ["a.jpg", "b.jpg"]
    assert files[0][1].isoformat() == "2026-03-02T09:15:00"


def test_import_dedupes_and_queues_vendor_categories(tmp_path, monkeypatch, capsys):
    make_export(tmp_path)
    bot, _ = make_bot()
    monkeypatch.setattr(backfill, "make_bot", lambda: bot)

    backfill.main(["import", str(tmp_path)])
    out = capsys.readouterr().out
    assert "duplicate 1" in out and "logged 1" in out
    [row] = bot.sheets.rows
    assert row["source"] == "telegram_backfill"
    assert row["received_at"] == "2026-03-02 09:15:00"
    [vendor] = bot.sheets.vendor_rows.values()
    assert vendor.category == ""

    vendor.category = "FOOD"  # the owner fills this in on the Vendors tab
    backfill.main(["apply-categories"])
    assert bot.sheets.rows[0]["category"] == "FOOD"


def test_dry_run_estimates_cost(tmp_path, monkeypatch, capsys):
    make_export(tmp_path)
    monkeypatch.setattr(backfill, "make_bot", lambda: (_ for _ in ()).throw(AssertionError("no API calls")))
    backfill.main(["import", str(tmp_path), "--dry-run"])
    assert "2 files, ~2 pages" in capsys.readouterr().out
