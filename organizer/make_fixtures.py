#!/usr/bin/env python3
"""Generate fixture_photos/ with ~16 synthetic JPEGs for testing.

Covers: several cameras, several dates (2012-2015), GPS EXIF (Rochester NY
+ NYC), files with no EXIF at all, one exact byte duplicate, one
resized/recompressed near-duplicate, one unrelated image, and one corrupt
.heic that Pillow cannot open.
"""
import hashlib
import os
import random
import shutil
import sys

from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(BASE, "fixture_photos")

ROCHESTER = (43.1566, -77.6088)
NYC = (40.7128, -74.0060)


def dms(deg):
    """Decimal degrees -> (deg, min, sec) floats for GPS EXIF."""
    d = int(abs(deg))
    m_f = (abs(deg) - d) * 60
    m = int(m_f)
    s = (m_f - m) * 60
    return (float(d), float(m), round(s, 2))


def make_pattern_image(seed, size=(240, 180), cell_upscale=True):
    """Deterministic 8x8 block pattern image -> stable, distinct aHashes."""
    rnd = random.Random(seed)
    small = Image.new("RGB", (8, 8))
    px = small.load()
    for y in range(8):
        for x in range(8):
            px[x, y] = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
    img = small.resize(size, Image.NEAREST)
    # a little extra structure so thumbnails aren't pure noise
    d = ImageDraw.Draw(img)
    d.rectangle([4, 4, size[0] // 3, size[1] // 3], outline=(255, 255, 255), width=2)
    return img


def build_exif(make=None, model=None, dt=None, gps=None):
    exif = Image.Exif()
    if make:
        exif[0x010F] = make
    if model:
        exif[0x0110] = model
    if dt:
        exif[0x0132] = dt
        exif.get_ifd(0x8769)[0x9003] = dt  # DateTimeOriginal
    if gps:
        lat, lon = gps
        g = exif.get_ifd(0x8825)
        g[1] = "N" if lat >= 0 else "S"
        g[2] = dms(lat)
        g[3] = "E" if lon >= 0 else "W"
        g[4] = dms(lon)
    return exif


def save(img, name, exif=None, quality=90):
    path = os.path.join(FIX, name)
    kwargs = {"quality": quality}
    if exif is not None:
        kwargs["exif"] = exif
    img.save(path, "JPEG", **kwargs)
    return path


def ahash(path):
    with Image.open(path) as im:
        g = im.convert("L").resize((8, 8), Image.LANCZOS)
        data = g.tobytes()
    mean = sum(data) / len(data)
    bits = 0
    for p in data:
        bits = (bits << 1) | (1 if p >= mean else 0)
    return bits


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    if os.path.isdir(FIX):
        shutil.rmtree(FIX)
    os.makedirs(FIX)

    jobs = [
        # name, seed, make, model, datetime, gps
        ("DSC_0001.jpg", 11, "NIKON CORPORATION", "NIKON D700", "2013:12:25 10:30:00", ROCHESTER),
        ("DSC_0002.jpg", 22, "NIKON CORPORATION", "NIKON D700", "2013:12:26 11:00:00", ROCHESTER),
        ("DSC_0003.jpg", 33, "NIKON CORPORATION", "NIKON D700", "2012:03:14 08:15:00", None),
        ("IMG_1001.jpg", 44, "Canon", "Canon EOS 5D", "2014:07:04 20:15:00", NYC),
        ("IMG_1002.jpg", 55, "Canon", "Canon EOS 5D", "2014:07:05 09:00:00", None),
        ("IMG_1003.jpg", 66, "Canon", "Canon EOS 5D", "2015:01:01 00:05:00", None),
        ("phone1.jpg", 77, "Apple", "iPhone 12", "2015:08:19 17:45:00", NYC),
        ("phone2.jpg", 88, "Apple", "iPhone 12", "2012:11:30 12:00:00", None),
        ("xmas2014.jpg", 99, "Canon", "Canon EOS 5D", "2014:12:25 09:00:00", None),  # same month-day as DSC_0001, different year
    ]
    for name, seed, make, model, dt, gps in jobs:
        img = make_pattern_image(seed)
        save(img, name, build_exif(make, model, dt, gps))

    # no EXIF at all
    for name, seed in [("plain1.jpg", 101), ("plain2.jpg", 102), ("plain3.jpg", 103)]:
        save(make_pattern_image(seed), name, exif=None)

    # duplicate master: Nikon, 2013-06-10
    master_img = make_pattern_image(200, size=(320, 240))
    m_exif = build_exif("NIKON CORPORATION", "NIKON D700", "2013:06:10 14:00:00", None)
    master = save(master_img, "master.jpg", m_exif)
    # exact byte copy
    exact = os.path.join(FIX, "master_exact_copy.jpg")
    shutil.copyfile(master, exact)
    # resized + recompressed near-duplicate (keeps EXIF)
    resized = master_img.resize((160, 120), Image.LANCZOS)
    save(resized, "master_resized.jpg", m_exif, quality=60)
    # unrelated image
    save(make_pattern_image(300), "unrelated.jpg",
         build_exif("Canon", "Canon EOS 5D", "2015:05:05 05:05:05", None))

    # corrupt HEIC (Pillow can't open) - must be listed gracefully
    with open(os.path.join(FIX, "broken.heic"), "wb") as f:
        f.write(os.urandom(256))

    # ---- verification ----
    print(f"Fixtures written to: {FIX}\n")
    ok = True
    for fn in sorted(os.listdir(FIX)):
        p = os.path.join(FIX, fn)
        if fn.endswith(".heic"):
            try:
                Image.open(p).load()
                print(f"  {fn}: unexpectedly opened!")
                ok = False
            except Exception as e:
                print(f"  {fn}: Pillow cannot open ({type(e).__name__}) -> graceful path OK")
            continue
        im = Image.open(p)
        e = im.getexif()
        cam = (e.get(0x010F), e.get(0x0110))
        dto = e.get_ifd(0x8769).get(0x9003)
        gps_ifd = dict(e.get_ifd(0x8825))
        print(f"  {fn}: {im.size} cam={cam} dto={dto} gps={'yes' if gps_ifd else 'no'}")

    m, c, r = (os.path.join(FIX, n) for n in ("master.jpg", "master_exact_copy.jpg", "master_resized.jpg"))
    same_bytes = md5(m) == md5(c)
    ham = bin(ahash(m) ^ ahash(r)).count("1")
    ham_unrel = bin(ahash(m) ^ ahash(os.path.join(FIX, "unrelated.jpg"))).count("1")
    print(f"\nexact copy md5 match: {same_bytes}")
    print(f"aHash hamming master vs resized: {ham} (need <= 6)")
    print(f"aHash hamming master vs unrelated: {ham_unrel} (need > 6)")
    if not (same_bytes and ham <= 6 and ham_unrel > 6 and ok):
        print("\nFIXTURE VERIFICATION FAILED", file=sys.stderr)
        sys.exit(1)
    print("\nAll fixture checks passed.")


if __name__ == "__main__":
    main()
    # NOTE: make_fixtures.py wipes fixture_photos/ - afterwards re-run
    #   python make_raw_fixtures.py
    # to regenerate the RAW/video/sidecar fixtures too.
