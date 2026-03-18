"""
core/models.py — Data model for a parsed academic paper.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from typing import List


@dataclass
class Author:
    name:         str = ""
    department:   str = ""
    organization: str = ""
    city:         str = ""
    country:      str = ""
    email:        str = ""


@dataclass
class Table:
    caption: str       = ""
    headers: List[str] = field(default_factory=list)
    rows:    List[List[str]] = field(default_factory=list)
    notes:   str       = ""


@dataclass
class Figure:
    caption:    str = ""
    image_path: str = ""
    label:      str = ""


@dataclass
class Reference:
    index: int = 0
    text:  str = ""


@dataclass
class Section:
    heading: str            = ""
    body:    str            = ""
    tables:  List[Table]    = field(default_factory=list)
    figures: List[Figure]   = field(default_factory=list)


@dataclass
class Document:
    title:      str             = "Untitled"
    authors:    List[Author]    = field(default_factory=list)
    abstract:   str             = ""
    keywords:   List[str]       = field(default_factory=list)
    sections:   List[Section]   = field(default_factory=list)
    references: List[Reference] = field(default_factory=list)

    # ── Serialisation ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, path: str) -> None:
        import os
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "Document":
        return cls(
            title    = d.get("title", "Untitled"),
            abstract = d.get("abstract", ""),
            keywords = d.get("keywords", []),
            authors  = [Author(**a) for a in d.get("authors", [])],
            sections = [Section(
                heading = s["heading"],
                body    = s["body"],
                tables  = [Table(**t) for t in s.get("tables", [])],
                figures = [Figure(**f) for f in s.get("figures", [])],
            ) for s in d.get("sections", [])],
            references = [Reference(**r) for r in d.get("references", [])],
        )

    @classmethod
    def from_json(cls, path: str) -> "Document":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
