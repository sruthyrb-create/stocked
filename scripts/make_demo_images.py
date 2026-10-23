"""Generate the illustrative demo snapshots in demo_images/ (a community fridge seen by a camera).

They are drawings, not photos: they let the demo and tests run without a Ring camera or AWS.
For the real thing, upload your own fridge photo in the simulator or connect a Ring camera.

    python scripts/make_demo_images.py
"""

import random
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "demo_images"
W, H = 960, 720
FRIDGE = (300, 70, 660, 690)  # x0, y0, x1, y1 of the fridge body
SHELVES = [(330, 110, 630, 250), (330, 270, 630, 410), (330, 430, 630, 570), (330, 590, 630, 670)]
COLORS = [(214, 69, 65), (241, 156, 56), (247, 202, 24), (92, 184, 92), (52, 152, 219), (155, 89, 182),
          (230, 126, 34), (46, 204, 113), (236, 240, 241), (139, 94, 60)]


def base(person: bool) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), (86, 92, 99))  # sidewalk / wall
    d = ImageDraw.Draw(img)
    d.rectangle((0, 560, W, H), fill=(120, 120, 118))  # pavement
    d.rounded_rectangle(FRIDGE, radius=18, fill=(205, 210, 214), outline=(60, 64, 70), width=6)
    d.rectangle((318, 92, 642, 680), fill=(244, 247, 249))  # bright, empty interior
    for x0, y0, x1, y1 in SHELVES:
        d.rectangle((x0 - 8, y1 + 2, x1 + 8, y1 + 10), fill=(180, 186, 192))  # shelf edge
    d.text((420, 30), "FREE FOOD - TAKE WHAT YOU NEED", fill=(255, 255, 255))
    if person:  # a passer-by outside the shelf area: the crop must exclude them
        d.ellipse((90, 220, 150, 280), fill=(40, 40, 46))
        d.rounded_rectangle((80, 285, 160, 520), radius=20, fill=(40, 40, 46))
    return img, d


def fill(d: ImageDraw.ImageDraw, fraction: float, rng: random.Random) -> None:
    for x0, y0, x1, y1 in SHELVES:
        x = x0 + 6
        while x < x1 - 30:
            if rng.random() < fraction:
                w = rng.randint(34, 62)
                h = rng.randint(int(0.5 * (y1 - y0)), (y1 - y0) - 10)
                color = rng.choice(COLORS)
                shape = rng.random()
                if shape < 0.4:
                    d.rounded_rectangle((x, y1 - h, x + w, y1), radius=6, fill=color, outline=(40, 40, 40), width=2)
                elif shape < 0.7:
                    d.ellipse((x, y1 - w, x + w, y1), fill=color, outline=(40, 40, 40), width=2)
                else:  # bottle
                    d.rectangle((x + w // 3, y1 - h - 14, x + 2 * w // 3, y1 - h), fill=color)
                    d.rounded_rectangle((x, y1 - h, x + w, y1), radius=10, fill=color, outline=(40, 40, 40), width=2)
                x += w + rng.randint(4, 10)
            else:
                x += rng.randint(30, 60)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, frac, person, spill in [("full", 0.95, True, False), ("half", 0.5, False, False),
                                      ("low", 0.14, True, False), ("empty", 0.0, False, False),
                                      ("spill", 0.45, False, True)]:
        rng = random.Random(name)
        img, d = base(person)
        fill(d, frac, rng)
        if spill:
            d.ellipse((360, 640, 560, 676), fill=(122, 74, 38))
        img.save(OUT / f"{name}.jpg", quality=88)
    print("wrote", sorted(p.name for p in OUT.glob("*.jpg")))


if __name__ == "__main__":
    main()
