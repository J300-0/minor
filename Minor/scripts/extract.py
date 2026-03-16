import fitz

def extract_pdf_text(input_path, output_path):
    doc = fitz.open(input_path)
    text = ""

    for page in doc:
        text += page.get_text()

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(text)

    print("Text extracted:", output_path)