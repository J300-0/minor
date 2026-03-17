"""
core/models.py  —  Shared Document dataclasses

All pipeline stages read/write these objects.
Serialises cleanly to/from JSON (structured.json).
"""

from dataclasses import dataclass, field, asdict
import json, os


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
    caption:  str  = ""
    headers:  list = field(default_factory=list)   # list[str]
    rows:     list = field(default_factory=list)   # list[list[str]]
    notes:    str  = ""


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
    heading: str  = ""
    body:    str  = ""
    tables:  list = field(default_factory=list)   # list[Table]
    figures: list = field(default_factory=list)   # list[Figure]


@dataclass
class Document:
    title:      str  = "Untitled"
    authors:    list = field(default_factory=list)   # list[Author]
    abstract:   str  = ""
    keywords:   list = field(default_factory=list)   # list[str]
    sections:   list = field(default_factory=list)   # list[Section]
    references: list = field(default_factory=list)   # list[Reference]

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    def to_dict_with_objects(self) -> dict:
        """Pass actual dataclass objects to Jinja2 (attribute access: ref.text, etc.)"""
        return {
            "title":      self.title,
            "authors":    self.authors,
            "abstract":   self.abstract,
            "keywords":   self.keywords,
            "sections":   self.sections,
            "references": self.references,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Document":
        authors = [Author(**a) if isinstance(a, dict) else a
                   for a in data.get("authors", [])]

        sections = []
        for s in data.get("sections", []):
            if isinstance(s, dict):
                tables  = [Table(**t)  if isinstance(t, dict) else t for t in s.get("tables",  [])]
                figures = [Figure(**f) if isinstance(f, dict) else f for f in s.get("figures", [])]
                sections.append(Section(
                    heading=s.get("heading", ""), body=s.get("body", ""),
                    tables=tables, figures=figures,
                ))
            else:
                sections.append(s)

        refs = []
        for i, r in enumerate(data.get("references", []), 1):
            if isinstance(r, dict):
                refs.append(Reference(**r))
            elif isinstance(r, str):
                refs.append(Reference(index=i, text=r))
            elif isinstance(r, Reference):
                refs.append(r)

        return cls(
            title=data.get("title", "Untitled"), authors=authors,
            abstract=data.get("abstract", ""), keywords=data.get("keywords", []),
            sections=sections, references=refs,
        )

    @classmethod
    def from_json(cls, path: str) -> "Document":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))