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
    page: int = -1             # page where figure appears (-1 = unknown)
    bbox_y: float = 0.0        # y-position on page for placement ordering


@dataclass
class FormulaBlock:
    latex: str = ""
    image_path: str = ""       # fallback when OCR unavailable — raw equation image
    confidence: float = 0.0
    page: int = 0
    label: str = ""
    bbox_y: float = 0.0        # y-position on page for placement ordering
    bbox_h: float = 0.0        # height of equation image for y-matching tolerance
    bbox_w: float = 0.0        # width of equation image
    equation_number: str = ""  # original equation number from input doc, e.g. "7"


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
    body_positions: list = field(default_factory=list)
    # List of (page, y) tuples — one per paragraph in body (split on \n\n).
    # Populated by the parser from block positions so the renderer can
    # interleave formula blocks at their correct source locations.


@dataclass
class Document:
    title: str = ""
    authors: List[Author] = field(default_factory=list)
    abstract: str = ""
    keywords: List[str] = field(default_factory=list)
    sections: List[Section] = field(default_factory=list)
    references: List[Reference] = field(default_factory=list)
    formula_blocks: List[FormulaBlock] = field(default_factory=list)
