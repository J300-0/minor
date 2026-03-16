from scripts.extract import extract_pdf_text
from scripts.convert import convert_to_markdown
from scripts.apply_template import apply_template
from scripts.compile import compile_latex

extract_pdf_text(
    "input/sample.pdf",
    "intermediate/extracted.txt"
)

convert_to_markdown(
    "intermediate/extracted.txt",
    "intermediate/converted.md"
)

apply_template(
    "intermediate/converted.md",
    "templates/ieee/template.tex",
    "intermediate/generated.tex"
)

compile_latex(
    "intermediate/generated.tex"
)