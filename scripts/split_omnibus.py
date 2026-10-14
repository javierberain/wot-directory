#!/usr/bin/env python3
"""
split_omnibus.py - Carve one book out of an omnibus EPUB into a standalone EPUB.

The omnibus TOC (toc.ncx) has one depth-1 navPoint per book with the chapters
nested beneath it. This script keeps only the spine items that belong to the
chosen book (from the book's first content file up to, but excluding, the next
depth-1 entry's content file), plus every CSS/image/font those files reference,
and writes a new content.opf and toc.ncx containing only that book's entries.
The rest of the omnibus (other books) is never read or printed.

    python scripts/split_omnibus.py --epub "books/omnibus.epub" \
        --book "CROSSROADS OF TWILIGHT" --out "books/Book_10_Crossroads_of_Twilight.epub" \
        --title "Crossroads of Twilight"
"""
import argparse, os, posixpath, re, sys, zipfile
from xml.sax.saxutils import escape

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epub", required=True)
    ap.add_argument("--book", required=True, help="exact depth-1 TOC label")
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--author", default="Robert Jordan")
    ap.add_argument("--list", action="store_true", help="print the book's chapter labels")
    a = ap.parse_args()

    z = zipfile.ZipFile(a.epub)
    names = set(z.namelist())
    opf_name = next(n for n in names if n.endswith(".opf"))
    ncx_name = next(n for n in names if n.endswith(".ncx"))
    base = posixpath.dirname(opf_name)
    def rel(p): return posixpath.join(base, p) if base else p

    opf = z.read(opf_name).decode("utf-8", "ignore")
    ncx = z.read(ncx_name).decode("utf-8", "ignore")

    # manifest id -> (href, media-type)
    manifest = {}
    for m in re.finditer(r"<item\b([^>]*)/?>", opf):
        attrs = dict(re.findall(r'(\S+)="([^"]*)"', m.group(1)))
        if "id" in attrs and "href" in attrs:
            manifest[attrs["id"]] = (attrs["href"], attrs.get("media-type", ""))
    spine = [m.group(1) for m in re.finditer(r'<itemref\b[^>]*idref="([^"]+)"', opf)]
    spine_hrefs = [manifest[i][0] for i in spine if i in manifest]

    # depth-1 navPoints with their raw xml blocks
    blocks = []  # (label, src, start, end)
    depth = 0; stack = []
    for m in re.finditer(r"<navPoint\b[^>]*>|</navPoint>", ncx):
        if m.group(0).startswith("<navPoint"):
            depth += 1
            if depth == 1: stack.append(m.start())
        else:
            if depth == 1:
                s = stack.pop(); e = m.end()
                blk = ncx[s:e]
                label = re.search(r"<text>(.*?)</text>", blk, re.S).group(1).strip()
                src = re.search(r'<content src="([^"#]+)', blk).group(1)
                blocks.append((label, src, s, e))
            depth -= 1
    idx = next((i for i, b in enumerate(blocks) if b[0] == a.book), None)
    if idx is None:
        sys.exit(f"book label not found: {a.book!r}")
    label, first_src, s, e = blocks[idx]
    next_src = blocks[idx + 1][1] if idx + 1 < len(blocks) else None
    block = ncx[s:e]

    if a.list:
        for t in re.findall(r"<text>(.*?)</text>", block, re.S)[1:]:
            print("  ", t.strip())

    # spine slice for this book
    i0 = spine_hrefs.index(first_src)
    i1 = spine_hrefs.index(next_src) if next_src else len(spine_hrefs)
    book_hrefs = spine_hrefs[i0:i1]

    # collect referenced assets (css, images, fonts) from those html files
    assets = set()
    for h in book_hrefs:
        html = z.read(rel(h)).decode("utf-8", "ignore")
        d = posixpath.dirname(h)
        for ref in re.findall(r'(?:href|src)="([^"#?]+)"', html):
            if ref.startswith(("http:", "https:", "mailto:")): continue
            p = posixpath.normpath(posixpath.join(d, ref))
            if rel(p) in names and p not in book_hrefs: assets.add(p)
    # fonts referenced from css
    for c in list(assets):
        if c.endswith(".css"):
            css = z.read(rel(c)).decode("utf-8", "ignore")
            for ref in re.findall(r"url\(['\"]?([^'\")]+)", css):
                p = posixpath.normpath(posixpath.join(posixpath.dirname(c), ref))
                if rel(p) in names: assets.add(p)

    href_to_id = {v[0]: (k, v[1]) for k, v in manifest.items()}
    def item(h):
        mid, mt = href_to_id.get(h, (re.sub(r"\W", "_", h), ""))
        if not mt:
            mt = {"css": "text/css", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                  "png": "image/png", "gif": "image/gif", "svg": "image/svg+xml",
                  "html": "application/xhtml+xml", "xhtml": "application/xhtml+xml",
                  "ttf": "application/x-font-ttf", "otf": "application/x-font-opentype"
                  }.get(h.rsplit(".", 1)[-1].lower(), "application/octet-stream")
        return mid, f'    <item id="{escape(mid)}" href="{escape(h)}" media-type="{mt}"/>'

    items, refs = ['    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'], []
    for h in book_hrefs:
        mid, line = item(h); items.append(line); refs.append(f'    <itemref idref="{escape(mid)}"/>')
    for h in sorted(assets):
        _, line = item(h); items.append(line)

    ident = re.sub(r"\W+", "-", a.title.lower())
    items_s = "\n".join(items); refs_s = "\n".join(refs)
    new_opf = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>{escape(a.title)}</dc:title>
    <dc:creator opf:role="aut">{escape(a.author)}</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier id="bookid">split-{ident}</dc:identifier>
  </metadata>
  <manifest>
{items_s}
  </manifest>
  <spine toc="ncx">
{refs_s}
  </spine>
</package>
'''
    # new ncx: header from the original up to <navMap>, then only this book's block
    head = ncx[:ncx.index("<navMap")]
    head = re.sub(r"<docTitle>.*?</docTitle>", f"<docTitle><text>{escape(a.title)}</text></docTitle>", head, flags=re.S)
    new_ncx = head + "<navMap>\n" + block + "\n</navMap>\n</ncx>\n"

    with zipfile.ZipFile(a.out, "w") as out:
        out.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        out.writestr("META-INF/container.xml", z.read("META-INF/container.xml"))
        out.writestr(opf_name, new_opf.encode("utf-8"))
        out.writestr(ncx_name, new_ncx.encode("utf-8"))
        for h in book_hrefs + sorted(assets):
            out.writestr(rel(h), z.read(rel(h)))
    print(f"wrote {a.out}: {len(book_hrefs)} content files, {len(assets)} assets")

if __name__ == "__main__":
    main()
