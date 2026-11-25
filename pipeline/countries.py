"""Sovereign-country list and name matching.

countries.json is generated from the `country-list` npm package that
rtd-travel-check uses, so every `name` here resolves to the right ISO code and
flag on the website. `aliases` covers common and state.gov spellings.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional

from .config import COUNTRIES_PATH


# Everyday names for email greetings; data.json keeps the official `name`.
DISPLAY = {
    "BO": "Bolivia", "CD": "the Democratic Republic of the Congo", "IR": "Iran", "KP": "North Korea",
    "KR": "the Republic of Korea", "FM": "the Federated States of Micronesia", "MD": "Moldova",
    "TZ": "Tanzania", "VE": "Venezuela", "TW": "Taiwan", "RU": "Russia", "SY": "Syria", "LA": "Laos",
    "VN": "Vietnam", "BN": "Brunei", "VA": "the Holy See", "GB": "the United Kingdom",
}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


class Countries:
    def __init__(self, path: Path = COUNTRIES_PATH):
        with open(path, encoding="utf-8") as f:
            self.items: List[dict] = json.load(f)
        self.by_code: Dict[str, dict] = {c["code"]: c for c in self.items}
        self._by_name: Dict[str, dict] = {}
        for c in self.items:
            for n in [c["name"], *c.get("aliases", [])]:
                self._by_name[norm(n)] = c

    def code_for(self, name: str) -> Optional[str]:
        c = self._by_name.get(norm(name))
        return c["code"] if c else None

    def name(self, code: str) -> str:
        return self.by_code[code]["name"]

    def display(self, code: str) -> str:
        return DISPLAY.get(code, self.by_code[code]["name"])

    def all_names(self, code: str) -> List[str]:
        c = self.by_code[code]
        return [c["name"], *c.get("aliases", [])]

    def covered_codes(self, data: List[dict]) -> set:
        """ISO codes of sovereign countries already present in data.json."""
        return {code for code in (self.code_for(e["country"]) for e in data) if code}
