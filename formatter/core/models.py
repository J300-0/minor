"""
core/models.py — Data model for a parsed academic paper.

Dataclasses: Document, Author, Section, Table, Figure, FormulaBlock, Reference.
All fields have safe defaults so partially-extracted papers never crash.

CHANGELOG:
  2026-03-22 — Added FormulaBlock for pix2tex math region results.
               Added formula_blocks field to Document.
"""
from __future__ import annotations

import json
import os
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
    caption: str             = ""
    headers: List[str]       = field(default_factory=list)
    rows:    List[List[str]] = field(default_factory=list)
    notes:   str             = ""


@dataclass
class Figure:
    caption:    str = ""
    image_path: str = ""
    label:      str = ""


@dataclass
class FormulaBlock:
    """
    A math region extracted from the PDF as a cropped PNG image,
    then converted to LaTeX by pix2tex.

    How it flows through the pipeline:
      Stage 1 (pdf_extractor) → math_extractor detects formula bounding boxes
                                  via PyMuPDF, crops them, calls pix2tex.
      Stage 1 result dict     → "formula_blocks": List[dict]
      Document                → formula_blocks: List[FormulaBlock]
      Stage 4 (renderer)      → inserts them as \\begin{equation}...\\end{equation}
                                  blocks at the point where the normalizer left a
                                  FORMULA_PLACEHOLDER marker.
    """
    latex:      str   = ""    # pix2tex output, e.g. r"\int_0^1 f(x)\,dx"
    image_path: str   = ""    # cropped PNG path — keep for debugging
    page:       int   = 0     # source page in the PDF
    confidence: float = 0.0   # pix2tex confidence (0.0–1.0); filter < 0.5
    label:      str   = ""    # auto-assigned: "eq:1", "eq:2", ...


@dataclass
class Reference:
    index: int = 0
    text:  str = ""


@dataclass
class Section:
    heading: str             = ""
    body:    str             = ""
    tables:  List[Table]     = field(default_factory=list)
    figures: List[Figure]    = field(default_factory=list)
    # depth: 1=\section, 2=\subsection, 3=\subsubsection
    depth:   int             = 1


@dataclass
class Document:
    title:          str                  = "Untitled"
    authors:        List[Author]         = field(default_factory=list)
    abstract:       str                  = ""
    keywords:       List[str]            = field(default_factory=list)
    sections:       List[Section]        = field(default_factory=list)
    references:     List[Reference]      = field(default_factory=list)
    formula_blocks: List[FormulaBlock]   = field(default_factory=list)

    # ── Serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> Document:
        return cls(
            title      = d.get("title", "Untitled"),
            abstract   = d.get("abstract", ""),
            keywords   = d.get("keywords", []),
            authors    = [Author(**a) for a in d.get("authors", [])],
            sections   = [
                Section(
                    heading = s["heading"],
                    body    = s["body"],
                    tables  = [Table(**t) for t in s.get("tables", [])],
                    figures = [Figure(**f) for f in s.get("figures", [])],
                    depth   = s.get("depth", 1),
                )
                for s in d.get("sections", [])
            ],
            references     = [Reference(**r) for r in d.get("references", [])],
            formula_blocks = [FormulaBlock(**fb)
                               for fb in d.get("formula_blocks", [])],
        )

    @classmethod
    def from_json(cls, path: str) -> Document:
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))