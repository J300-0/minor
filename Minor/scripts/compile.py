import subprocess

def compile_latex(tex_file):

    cmd = [
        "pdflatex",
        "-output-directory",
        "output",
        tex_file
    ]

    subprocess.run(cmd)

    print("PDF generated in output/")