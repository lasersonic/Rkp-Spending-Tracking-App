"""Spend categories and vendor memory.

A vendor is matched by tax ID first, then by normalized name (or one of its
aliases). Once a user picks a category for a vendor, every later receipt from
that vendor is categorized automatically.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Category:
    code: str
    name: str
    group: str


DEFAULT_CATEGORIES: list[Category] = [
    Category("FOOD", "Food cost (วัตถุดิบ)", "COGS"),
    Category("BEV", "Beverages", "COGS"),
    Category("PACK", "Packaging", "COGS"),
    Category("RENT", "Rent", "Opex"),
    Category("UTIL", "Utilities (electricity, water, gas)", "Opex"),
    Category("NET", "Internet & phone", "Opex"),
    Category("STAFF", "Staff", "Opex"),
    Category("EQUIP", "Equipment & repair", "Opex"),
    Category("CLEAN", "Cleaning & supplies", "Opex"),
    Category("MKT", "Marketing", "Opex"),
    Category("OTHER", "Other", "Opex"),
]


def short_name(category: Category) -> str:
    """'Utilities (electricity, water, gas)' -> 'Utilities'; LINE labels max 20 chars."""
    return category.name.split(" (")[0][:20]


# Company-form words that vary between receipts from the same vendor.
_NOISE = [
    r"บริษัท", r"บจก\.?", r"บมจ\.?", r"ห้างหุ้นส่วนจำกัด", r"ห้างหุ้นส่วน", r"หจก\.?",
    r"จำกัด", r"\(มหาชน\)", r"มหาชน", r"สำนักงานใหญ่", r"สาขา.*$",
    r"\bco\b\.?", r"\bcompany\b", r"\bltd\b\.?", r"\blimited\b", r"\bplc\b\.?",
    r"\binc\b\.?", r"\bpart\b\.?", r"\bhead office\b", r"\bbranch\b.*$",
]
_NOISE_RE = re.compile("|".join(_NOISE), re.IGNORECASE)


def normalize_vendor_name(name: str) -> str:
    text = _NOISE_RE.sub(" ", name or "").lower()
    # Drop spaces, punctuation and symbols but keep Thai vowel and tone marks.
    return "".join(ch for ch in text if unicodedata.category(ch)[0] in "LMN")


@dataclass
class Vendor:
    vendor_key: str
    tax_id: str = ""
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    category: str = ""
    first_seen: str = ""
    receipt_count: int = 0

    def name_keys(self) -> set[str]:
        keys = {normalize_vendor_name(self.name)}
        keys.update(normalize_vendor_name(a) for a in self.aliases)
        keys.discard("")
        return keys


def vendor_key(tax_id: str, name: str) -> str:
    if tax_id:
        return f"tax:{tax_id}"
    return f"name:{normalize_vendor_name(name)}"


def find_vendor(vendors: list[Vendor], tax_id: str, name: str) -> Vendor | None:
    if tax_id:
        for v in vendors:
            if v.tax_id == tax_id:
                return v
    key = normalize_vendor_name(name)
    if key:
        for v in vendors:
            if key in v.name_keys():
                return v
    return None
