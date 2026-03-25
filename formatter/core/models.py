"""
core/models.py — Data classes for the pipeline.

Every stage communicates through these models.
"""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Author:
    name: str = ""
    department: str = ""
    organization: str = ""
    city: str = ""
    country: str = ""
    email: str = ""


@dataclass
class Table:
    headers: List[str] = field(default_factory=list)
    rows: List[List[str]] = field(default_factory=list)
    caption: str = ""
    label: str = ""


@dataclass
class Figure:
    image_path: str = ""
    caption: str = ""
    label: str = ""


@dataclass
class FormulaBlock:
    latex: str = ""
    image_path: str = ""       # fallback when OCR unavailable — raw equation image
    confidence: float = 0.0
    page: int = 0
    label: str = ""
    bbox_y: float = 0.0        # y-position on page for placement ordering


@dataclass
class Reference:
    text: str = ""
    index: int = 0
    author_year: str = ""


@dataclass
class Section:
    heading: str = ""
    depth: int = 1          # 1 = section, 2 = subsection, 3 = subsubsection
    body: str = ""
    tables: List[Table] = field(default_factory=list)
    figures: List[Figure] = field(default_factory=list)
    formula_blocks: List['FormulaBlock'] = field(default_factory=list)
    start_page: int = -1     # page where this section's heading appears (-1 = unknown)


@dataclass
class Document:
    title: str = ""
    authors: List[Author] = field(default_factory=list)
    abstract: str = ""
    keywords: List[str] = field(default_factory=list)
    sections: List[Section] = field(default_factory=list)
    references: List[Reference] = field(default_factory=list)
    formula_blocks: List[FormulaBlock] = field(default_factory=list)
