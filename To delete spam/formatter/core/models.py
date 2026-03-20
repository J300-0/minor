"""
core/models.py
Typed dataclasses for the pipeline's intermediate Document representation.

Author now carries the full IEEE author block fields:
  name / department / organization / city / country / email
These map directly to \IEEEauthorblockN and \IEEEauthorblockA in the template.
All fields are optional strings — leave blank if not available in the source.
"""

from dataclasses import dataclass, field, asdict
import json
import os


@dataclass
class Author:
    name:         str = ""
    department:   str = ""   # \textit{dept. name of organization}
    organization: str = ""   # \textit{name of organization}
    city:         str = ""
    country:      str = ""
    email:        str = ""


@dataclass
class Section:
    heading: str
    body:    str


@dataclass
class Document:
    title:      str           = "Untitled"
    authors:    list[Author]  = field(default_factory=list)
    abstract:   str           = ""
    keywords:   list[str]     = field(default_factory=list)
    sections:   list[Section] = field(default_factory=list)
    references: list[str]     = field(default_factory=list)

    # ── Serialization ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "Document":
        authors = [
            Author(**a) if isinstance(a, dict) else a
            for a in data.get("authors", [])
        ]
        sections = [
            Section(**s) if isinstance(s, dict) else s
            for s in data.get("sections", [])
        ]
        return cls(
            title      = data.get("title", "Untitled"),
            authors    = authors,
            abstract   = data.get("abstract", ""),
            keywords   = data.get("keywords", []),
            sections   = sections,
            references = data.get("references", []),
        )

    @classmethod
    def from_json(cls, path: str) -> "Document":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))