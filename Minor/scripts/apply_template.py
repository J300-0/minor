def apply_template(md_file, template_file, output_tex):

    with open(md_file) as f:
        content = f.read()

    with open(template_file) as f:
        template = f.read()

    tex = template.replace("$body$", content)

    with open(output_tex, "w") as f:
        f.write(tex)

    print("LaTeX generated:", output_tex)