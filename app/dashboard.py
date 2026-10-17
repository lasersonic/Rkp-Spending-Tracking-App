"""Dashboard tab: live QUERY formulas over Receipts and LineItems.

Blocks sit side by side because each one grows downward as months pass.
Month labels use YEAR()/MONTH() so they are always Christian-era, whatever
the spreadsheet's locale.
"""

from __future__ import annotations

from .storage import LINE_ITEM_COLUMNS, RECEIPT_COLUMNS, col_letter


def _r(column: str) -> str:
    letter = col_letter(RECEIPT_COLUMNS.index(column))
    return f"Receipts!{letter}2:{letter}"


def _li(column: str) -> str:
    letter = col_letter(LINE_ITEM_COLUMNS.index(column))
    return f"LineItems!{letter}2:{letter}"


def _month(date_range: str) -> str:
    return f'ARRAYFORMULA(IF({date_range}="","",YEAR({date_range})&"-"&TEXT(MONTH({date_range}),"00")))'


MONTH = _month(_r("receipt_date"))
# Month of each line item, looked up from its receipt.
ITEM_MONTH = _month(
    f'ARRAYFORMULA(IFERROR(VLOOKUP({_li("receipt_id")},{{{_r("receipt_id")},{_r("receipt_date")}}},2,FALSE),""))'
)

BLOCKS: list[tuple[str, str, str]] = [
    (
        "A",
        "Monthly spend by category (฿)",
        f'=QUERY({{{MONTH},{_r("category")},{_r("grand_total")}}},'
        f'"select Col1, sum(Col3) where Col1 <> \'\' and Col3 is not null '
        f'group by Col1 pivot Col2 order by Col1 label Col1 \'Month\'",0)',
    ),
    (
        "Q",
        "Food cost trend (FOOD, ฿)",
        f'=QUERY({{{MONTH},{_r("category")},{_r("grand_total")}}},'
        f'"select Col1, sum(Col3) where Col2 = \'FOOD\' and Col1 <> \'\' '
        f'group by Col1 order by Col1 label Col1 \'Month\', sum(Col3) \'Food cost\'",0)',
    ),
    (
        "T",
        "Input VAT by month (for ภ.พ.30)",
        f'=QUERY({{{MONTH},{_r("net_before_vat")},{_r("vat")},{_r("grand_total")}}},'
        f'"select Col1, count(Col3), sum(Col2), sum(Col3), sum(Col4) where Col3 > 0 and Col1 <> \'\' '
        f'group by Col1 order by Col1 label Col1 \'Month\', count(Col3) \'Tax invoices\', '
        f'sum(Col2) \'Net before VAT\', sum(Col3) \'Input VAT\', sum(Col4) \'Total incl. VAT\'",0)',
    ),
    (
        "Z",
        "Top vendors (all time)",
        f'=QUERY({{{_r("vendor_name")},{_r("grand_total")}}},'
        f'"select Col1, sum(Col2), count(Col2) where Col1 <> \'\' and Col2 is not null '
        f'group by Col1 order by sum(Col2) desc limit 20 '
        f'label Col1 \'Vendor\', sum(Col2) \'Spend\', count(Col2) \'Receipts\'",0)',
    ),
    (
        "AD",
        "Item unit cost by month (avg ฿)",
        f'=QUERY({{{_li("item_name")},{ITEM_MONTH},{_li("unit_cost")}}},'
        f'"select Col1, avg(Col3) where Col1 <> \'\' and Col2 <> \'\' and Col3 is not null '
        f'group by Col1 pivot Col2 label Col1 \'Item\'",0)',
    ),
]


def write_dashboard(ws) -> None:
    updates = []
    for col, title, formula in BLOCKS:
        updates.append({"range": f"{col}1", "values": [[title]]})
        updates.append({"range": f"{col}2", "values": [[formula]]})
    ws.batch_update(updates, value_input_option="USER_ENTERED")
    ws.format("1:1", {"textFormat": {"bold": True}})
