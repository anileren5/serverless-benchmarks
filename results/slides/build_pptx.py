import json, sys
from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR

S = sys.argv[1]
spec = json.load(open(f"{S}/deck/batch1.json"))
notes = json.load(open(f"{S}/deck/notes.json"))
texts = {}
for r in spec["requests"]:
    if "insertText" in r and r["insertText"]["text"]:
        texts[r["insertText"]["objectId"]] = r["insertText"]["text"]
INK = RGBColor(0x11, 0x11, 0x11)

prs = Presentation()
prs.slide_width, prs.slide_height = Inches(10), Inches(5.625)
blank = prs.slide_layouts[6]

def box(slide, x, y, w, h, text, size, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, space=4):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    for i, line in enumerate(text.split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        level = len(line) - len(line.lstrip("\t"))
        p.level = level
        p.alignment = align
        p.space_after = Pt(space)
        run = p.add_run()
        run.text = line.lstrip("\t")
        run.font.name = "Arial"
        run.font.size = Pt(size)
        run.font.color.rgb = INK
        if level:
            p._pPr.set("marL", str(int(Inches(0.35 * level))))
    return tb

def slide_with(key, title=True):
    s = prs.slides.add_slide(blank)
    s.notes_slide.notes_text_frame.text = notes[key]
    if title:
        box(s, 0.6, 0.21, 8.8, 0.6, texts[key + "_title"], 18, anchor=MSO_ANCHOR.MIDDLE)
    return s

s = slide_with("pe_section", title=False)
box(s, 0.6, 2.35, 8.8, 0.6, texts["pe_section_title"], 18, align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)

s = slide_with("pe_sofar")
box(s, 0.6, 1.05, 8.8, 4.0, texts["pe_sofar_body"], 14)

FIG = f"{S}/figs/"
for key, image in (("pe_cpus", "slide_effective_cpus"), ("pe_storm", "slide_barrier_storm"),
                   ("pe_onnx", "slide_onnx")):
    s = slide_with(key)
    s.shapes.add_picture(FIG + image + ".png", Inches(0.35), Inches(0.95), Inches(6.2), Inches(3.9))
    box(s, 6.7, 1.05, 2.95, 3.9, texts[key + "_body"], 12, space=6)

s = slide_with("pe_gang")
w = 8.4
s.shapes.add_picture(FIG + "slide_gang_scheduling.png", Inches(0.8), Inches(0.85), Inches(w), Inches(w * 3.7 / 9.2))
box(s, 0.6, 4.3, 8.8, 0.95, texts["pe_gang_body"], 12, space=3)

s = slide_with("pe_next")
box(s, 0.6, 1.05, 8.8, 4.0, texts["pe_next_body"], 14, space=6)

out = f"{S}/deck/pushing_the_envelope.pptx"
prs.save(out)
print(out)
