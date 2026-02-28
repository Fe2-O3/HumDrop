#!/usr/bin/env python3
"""Generate HumDrop app icons (.ico and .icns) matching the in-app bird design."""

import math
import os
import subprocess
import sys
from PIL import Image, ImageDraw

# Colors matching humdrop.py
TEAL = (26, 184, 158)       # #1AB89E
TEAL_DARK = (21, 143, 122)  # #158F7A
TEAL_LIGHT = (40, 210, 180) # lighter teal for gradient
ORANGE = (232, 134, 58)     # #E8863A
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


def draw_bird_icon(size: int) -> Image.Image:
    """Draw the HumDrop bird icon at the given pixel size."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    s = size  # shorthand

    # --- Rounded rectangle background ---
    corner = int(s * 0.18)
    # Deep teal base
    draw.rounded_rectangle([0, 0, s - 1, s - 1], radius=corner, fill=TEAL_DARK)
    # Lighter teal overlay (upper portion for gradient feel)
    draw.rounded_rectangle([0, 0, s - 1, int(s * 0.65)], radius=corner, fill=TEAL)
    # Bottom blend rectangle (no corners) to smooth the join
    draw.rectangle([0, int(s * 0.45), s - 1, int(s * 0.65)], fill=TEAL)

    # Subtle white border
    for i in range(max(1, int(s * 0.005))):
        draw.rounded_rectangle(
            [i, i, s - 1 - i, s - 1 - i],
            radius=corner,
            outline=(*WHITE, 50),
        )

    # --- Branch ---
    branch_y = int(s * 0.52)
    branch_thick = max(2, int(s * 0.028))
    branch_color = (80, 60, 40)  # brown

    # Main branch — slight curve via segments
    points = []
    for i in range(20):
        t = i / 19.0
        x = int(s * 0.12 + t * s * 0.76)
        y = int(branch_y + math.sin(t * math.pi) * s * 0.03)
        points.append((x, y))
    for i in range(len(points) - 1):
        draw.line([points[i], points[i + 1]], fill=branch_color, width=branch_thick)

    # Small twig going up-right
    twig_start = points[13]
    twig_end = (twig_start[0] + int(s * 0.06), twig_start[1] - int(s * 0.07))
    draw.line([twig_start, twig_end], fill=branch_color, width=max(1, branch_thick // 2))

    # --- Bird body ---
    bx = int(s * 0.48)  # bird center x
    by = int(s * 0.38)  # bird center y
    bw = int(s * 0.13)  # body half-width
    bh = int(s * 0.10)  # body half-height

    # Body (white ellipse)
    draw.ellipse([bx - bw, by - bh, bx + bw, by + bh], fill=WHITE)

    # Head
    hr = int(s * 0.068)
    hx = bx + int(bw * 0.7)
    hy = by - int(bh * 0.5)
    draw.ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill=WHITE)

    # Eye
    er = max(1, int(s * 0.014))
    ex = hx + int(hr * 0.3)
    ey = hy - int(hr * 0.15)
    draw.ellipse([ex - er, ey - er, ex + er, ey + er], fill=BLACK)
    # Eye highlight
    ehr = max(1, er // 2)
    draw.ellipse([ex - ehr, ey - ehr - 1, ex, ey - 1], fill=WHITE)

    # Orange beak
    beak_len = int(s * 0.05)
    beak_pts = [
        (hx + hr, hy - int(hr * 0.15)),
        (hx + hr + beak_len, hy),
        (hx + hr, hy + int(hr * 0.2)),
    ]
    draw.polygon(beak_pts, fill=ORANGE)

    # Tail feathers (left side of body)
    tail_pts = [
        (bx - bw, by - int(bh * 0.3)),
        (bx - bw - int(s * 0.07), by - int(bh * 0.8)),
        (bx - bw - int(s * 0.05), by - int(bh * 0.1)),
        (bx - bw - int(s * 0.08), by + int(bh * 0.2)),
        (bx - bw + int(s * 0.01), by + int(bh * 0.3)),
    ]
    draw.polygon(tail_pts, fill=WHITE)

    # Wing detail
    wing_pts = [
        (bx - int(bw * 0.2), by - int(bh * 0.3)),
        (bx + int(bw * 0.1), by),
        (bx - int(bw * 0.3), by + int(bh * 0.5)),
    ]
    wing_line_color = (*TEAL_DARK, 120)
    draw.line(wing_pts, fill=wing_line_color, width=max(1, int(s * 0.008)))

    # Legs
    leg_w = max(1, int(s * 0.008))
    foot_x1 = bx + int(bw * 0.1)
    foot_x2 = bx + int(bw * 0.4)
    foot_top = by + bh - int(s * 0.01)
    foot_bottom = branch_y - max(1, branch_thick // 2)
    draw.line([(foot_x1, foot_top), (foot_x1, foot_bottom)], fill=(80, 80, 80), width=leg_w)
    draw.line([(foot_x2, foot_top), (foot_x2, foot_bottom)], fill=(80, 80, 80), width=leg_w)

    # --- Download arrow icon (below branch) ---
    arrow_cx = int(s * 0.50)
    arrow_top = int(s * 0.62)
    arrow_bottom = int(s * 0.82)
    shaft_w = max(2, int(s * 0.035))
    head_w = int(s * 0.10)
    head_h = int(s * 0.06)

    # Shaft
    draw.rectangle(
        [arrow_cx - shaft_w // 2, arrow_top, arrow_cx + shaft_w // 2, arrow_bottom - head_h],
        fill=WHITE,
    )

    # Arrowhead triangle
    arrow_pts = [
        (arrow_cx - head_w // 2, arrow_bottom - head_h),
        (arrow_cx + head_w // 2, arrow_bottom - head_h),
        (arrow_cx, arrow_bottom),
    ]
    draw.polygon(arrow_pts, fill=WHITE)

    # Tray line (horizontal bar under arrow)
    tray_y = int(s * 0.86)
    tray_w = int(s * 0.22)
    tray_thick = max(2, int(s * 0.02))
    draw.line(
        [(arrow_cx - tray_w // 2, tray_y), (arrow_cx + tray_w // 2, tray_y)],
        fill=WHITE,
        width=tray_thick,
    )
    # Small vertical edges on tray
    tray_edge_h = int(s * 0.03)
    draw.line(
        [(arrow_cx - tray_w // 2, tray_y), (arrow_cx - tray_w // 2, tray_y - tray_edge_h)],
        fill=WHITE,
        width=tray_thick,
    )
    draw.line(
        [(arrow_cx + tray_w // 2, tray_y), (arrow_cx + tray_w // 2, tray_y - tray_edge_h)],
        fill=WHITE,
        width=tray_thick,
    )

    return img


def main():
    out_dir = os.path.dirname(os.path.abspath(__file__))

    # Generate at multiple sizes for crisp icons
    sizes_ico = [16, 32, 48, 64, 128, 256]
    sizes_icns = [16, 32, 64, 128, 256, 512, 1024]

    # --- Windows .ico ---
    # PIL ICO needs images provided at exactly the target sizes
    ico_images = []
    for sz in sizes_ico:
        img = draw_bird_icon(sz).convert("RGBA")
        ico_images.append(img)
    ico_path = os.path.join(out_dir, "HumDrop.ico")
    # Save with the largest image first; append the rest
    ico_images[-1].save(
        ico_path,
        format="ICO",
        append_images=ico_images[:-1],
        sizes=[(sz, sz) for sz in sizes_ico],
    )
    print(f"Created {ico_path}")

    # --- macOS .icns via iconutil ---
    iconset_dir = os.path.join(out_dir, "HumDrop.iconset")
    os.makedirs(iconset_dir, exist_ok=True)

    icns_map = {
        "icon_16x16.png": 16,
        "icon_16x16@2x.png": 32,
        "icon_32x32.png": 32,
        "icon_32x32@2x.png": 64,
        "icon_128x128.png": 128,
        "icon_128x128@2x.png": 256,
        "icon_256x256.png": 256,
        "icon_256x256@2x.png": 512,
        "icon_512x512.png": 512,
        "icon_512x512@2x.png": 1024,
    }

    for name, sz in icns_map.items():
        img = draw_bird_icon(sz)
        img.save(os.path.join(iconset_dir, name), format="PNG")

    icns_path = os.path.join(out_dir, "HumDrop.icns")
    try:
        subprocess.run(
            ["iconutil", "-c", "icns", iconset_dir, "-o", icns_path],
            check=True, capture_output=True,
        )
        print(f"Created {icns_path}")
    except FileNotFoundError:
        print("iconutil not found (not macOS?) — skipping .icns generation")
    except subprocess.CalledProcessError as e:
        print(f"iconutil failed: {e.stderr.decode()}")

    # Clean up iconset
    import shutil
    shutil.rmtree(iconset_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
