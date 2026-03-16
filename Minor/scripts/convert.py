import subprocess

def convert_to_markdown(input_file, output_file):

    cmd = [
        "pandoc",
        input_file,
        "-t",
        "markdown",
        "-o",
        output_file
    ]

    subprocess.run(cmd)

    print("Converted to markdown:", output_file)