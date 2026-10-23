#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow"]
# ///
"""Portrait atlas for the community map: one 64px square per interviewee with a picture.

  uv run analysis/fetch_portraits.py

Sources, in order: the Linked Jazz / semlab square thumbnail (`wd_people.semlab_thumb_url`),
then the Wikidata P18 image on Commons (`image_url`, fetched as a 128px thumbnail through
Special:FilePath?width=). Originals are cached under data/portraits/<qid>.* so a rerun only
fetches what is new. Writes

  site/assets/portraits.jpg      the atlas, COLS x rows cells of CELL px
  site/data/portraits.json       {"cell": 64, "cols": N, "qids": {qid: index}}
"""
import io, json, sqlite3, sys, time, urllib.request
from pathlib import Path
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT.parent / "ch-jazz-mashup" / "linked_jazz.sqlite"
CACHE = ROOT / "data" / "portraits"; CACHE.mkdir(parents=True, exist_ok=True)
CELL, COLS = 64, 24
UA = "linked-jazz-segments/0.1 (https://github.com/thisismattmiller/linked-jazz-segments; portrait atlas for the community map)"

con = sqlite3.connect(DB)
rows = con.execute("""
  SELECT DISTINCT w.qid, w.semlab_thumb_url, w.image_url
  FROM documents d JOIN doc_interviewees di ON di.doc_id = d.doc_id AND di.role = 'principal'
  JOIN wd_people w ON w.qid = di.qid
  WHERE d.num_segments > 0 AND (w.semlab_thumb_url IS NOT NULL OR w.image_url IS NOT NULL)
  ORDER BY w.qid""").fetchall()
print(f"{len(rows)} interviewees with a picture source", file=sys.stderr)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def fetch(qid, thumb, commons):
    for p in CACHE.glob(f"{qid}.*"):
        if p.stat().st_size > 0:
            return p
    cands = []
    if thumb: cands.append(thumb)
    if commons:
        url = commons.replace("http://", "https://")
        cands.append(url + ("&" if "?" in url else "?") + "width=128")
    for url in cands:
        try:
            data = get(url)
            im = Image.open(io.BytesIO(data)); im.load()
            p = CACHE / f"{qid}.{(im.format or 'png').lower()}"
            p.write_bytes(data)
            time.sleep(0.25)
            return p
        except Exception as e:
            print(f"  {qid}: {url} -> {e}", file=sys.stderr)
            time.sleep(0.5)
    return None


paths = {}
for i, (qid, thumb, commons) in enumerate(rows):
    p = fetch(qid, thumb, commons)
    if p: paths[qid] = p
    if i % 50 == 0: print(f"  {i}/{len(rows)} fetched, {len(paths)} ok", file=sys.stderr)

qids = sorted(paths)
nrows = (len(qids) + COLS - 1) // COLS
atlas = Image.new("RGB", (COLS * CELL, nrows * CELL), (220, 216, 208))
index = {}
for i, qid in enumerate(qids):
    try:
        im = Image.open(paths[qid]).convert("RGB")
        im = ImageOps.fit(im, (CELL, CELL), Image.LANCZOS, centering=(0.5, 0.3))  # faces sit high
        atlas.paste(im, ((i % COLS) * CELL, (i // COLS) * CELL))
        index[qid] = i
    except Exception as e:
        print(f"  {qid}: unreadable ({e})", file=sys.stderr)
atlas.save(ROOT / "site" / "assets" / "portraits.jpg", quality=82, optimize=True)
json.dump({"cell": CELL, "cols": COLS, "qids": index}, open(ROOT / "site" / "data" / "portraits.json", "w"))
print(f"atlas: {len(index)} portraits, {COLS}x{nrows} cells", file=sys.stderr)
