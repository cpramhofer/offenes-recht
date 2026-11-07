#!/usr/bin/env python3
"""Baut static/index.html aus head.html, src/extra.css, src/body.html und src/app.js."""
import os
H = os.path.dirname(os.path.abspath(__file__))
r = lambda p: open(os.path.join(H, p), encoding="utf-8").read()
head = r("head.html").replace("</style>\n\n", "</style>\n", 1)
import base64, glob
# Schriften direkt einbetten (keine Verbindung zu Google Fonts; Lizenz: SIL OFL, siehe src/fonts)
faces = []
for f in sorted(glob.glob(os.path.join(H, "src", "fonts", "*.woff2"))):
    n = os.path.basename(f)
    fam = "Literata" if n.startswith("literata") else "IBM Plex Sans"
    w = n.split("-")[-2]
    faces.append("@font-face{font-family:'%s';font-style:normal;font-weight:%s;font-display:swap;src:url(data:font/woff2;base64,%s) format('woff2')}"
                 % (fam, w, base64.b64encode(open(f, "rb").read()).decode()))
fontcss = "<style>\n" + "\n".join(faces) + "\n</style>\n"
html = (head + fontcss + "<style>\n" + r("src/extra.css") + "</style>\n" + r("src/body.html") +
        "<script>\n" + r("src/app.js") + "</script>\n</body></html>\n")
os.makedirs(os.path.join(H, "static"), exist_ok=True)
open(os.path.join(H, "static", "index.html"), "w", encoding="utf-8").write(html)
print("static/index.html", len(html), "Bytes")
