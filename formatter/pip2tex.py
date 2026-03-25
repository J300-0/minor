from PIL import Image
from pix2tex.cli import LatexOCR

img = Image.open('C:/Users/janme/Pictures/Screenshots/Screenshot 2026-03-25 172333.png')
model = LatexOCR()
print(model(img))