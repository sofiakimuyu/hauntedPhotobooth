import argparse
import json
import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Dict, Any, Tuple

from PIL import Image


@dataclass
class Slot:
    id: int
    x: int
    y: int
    w: int
    h: int
    rotation: int = 0


def project_dir() -> str:
    # Folder where this script lives (safe for GitHub / cross-platform)
    return os.path.dirname(os.path.abspath(__file__))


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def cover_resize(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """
    Resize to fill (cover) target box while preserving aspect ratio, then center-crop.
    No stretching.
    """
    iw, ih = img.size
    if iw <= 0 or ih <= 0:
        raise ValueError("Invalid image size.")

    scale = max(target_w / iw, target_h / ih)
    nw, nh = int(round(iw * scale)), int(round(ih * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)

    left = (nw - target_w) // 2
    top = (nh - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def parse_slots(data: Dict[str, Any], json_path: str) -> List[Slot]:
    """
    Supports either:
      - full slots with x,y,w,h in each slot
      - OR a top-level "defaults" dict merged into each slot
    """
    defaults = data.get("defaults", {}) or {}
    slots_raw = data.get("slots", [])

    if not isinstance(slots_raw, list) or len(slots_raw) != 4:
        raise ValueError(f"{json_path}: expected exactly 4 slots in 'slots'.")

    slots: List[Slot] = []
    for i, s in enumerate(slots_raw):
        if not isinstance(s, dict):
            raise ValueError(f"{json_path}: slot index {i} is not an object/dict.")

        merged = {**defaults, **s}  # slot overrides defaults
        missing = [k for k in ("id", "x", "y", "w", "h") if k not in merged]
        if missing:
            raise KeyError(f"{json_path}: slot index {i} missing keys: {missing}. Slot={s}, defaults={defaults}")

        slots.append(Slot(
            id=int(merged["id"]),
            x=int(merged["x"]),
            y=int(merged["y"]),
            w=int(merged["w"]),
            h=int(merged["h"]),
            rotation=int(merged.get("rotation", 0)),
        ))

    slots.sort(key=lambda sl: sl.id)
    return slots


def compose_into_template(template_png_path: str, slots: List[Slot], photo_paths: List[str]) -> Image.Image:
    """
    Base = template PNG. Photos pasted into slots on top of it.
    If you later want an overlay-only workflow, tell me and I’ll give that variant.
    """
    base = Image.open(template_png_path).convert("RGBA")

    if len(photo_paths) != 4:
        raise ValueError("Expected exactly 4 photos.")

    for slot, photo_path in zip(slots, photo_paths):
        img = Image.open(photo_path).convert("RGBA")
        if slot.rotation:
            img = img.rotate(slot.rotation, expand=True)
        fitted = cover_resize(img, slot.w, slot.h)
        base.paste(fitted, (slot.x, slot.y), fitted)

    return base


def side_by_side(img: Image.Image, gap: int = 40, bg: Tuple[int, int, int, int] = (255, 255, 255, 255)) -> Image.Image:
    """
    Duplicate the composed template image and place two copies side-by-side with a gap.
    """
    w, h = img.size
    out = Image.new("RGBA", (w * 2 + gap, h), bg)
    out.paste(img, (0, 0), img)
    out.paste(img, (w + gap, 0), img)
    return out


def preview(img: Image.Image) -> None:
    """
    Cross-platform preview using the default image viewer.
    """
    img.show()


def save_for_print(img: Image.Image, out_path: str, jpeg_quality: int = 95) -> str:
    """
    Save to a file path. Uses JPG by default for easiest printing.
    """
    ext = os.path.splitext(out_path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        img_rgb = img.convert("RGB")
        img_rgb.save(out_path, "JPEG", quality=jpeg_quality)
    elif ext == ".png":
        img.save(out_path, "PNG")
    else:
        # default to jpg if user gave no/unknown extension
        out_path = out_path + ".jpg"
        img.convert("RGB").save(out_path, "JPEG", quality=jpeg_quality)
    return out_path


def try_print(file_path: str, printer_name: str, copies: int = 1) -> None:
    """
    Best-effort cross-platform printing.
    - macOS: uses lp
    - Windows: uses mspaint /pt
    """
    system = platform.system().lower()

    if copies < 1:
        copies = 1

    if "darwin" in system or "mac" in system:
        # macOS: lp is the normal CLI print path
        # -n copies sets number of copies
        cmd = ["lp", "-d", printer_name, "-n", str(copies), file_path]
        subprocess.run(cmd, check=True)

    elif "windows" in system:
        # Windows: mspaint /pt prints images to a named printer
        # This will spawn briefly; depends on printer driver availability.
        # Copies: mspaint doesn't reliably support copies via CLI; loop copies.
        mspaint = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "mspaint.exe")
        if not os.path.exists(mspaint):
            mspaint = "mspaint"

        for _ in range(copies):
            cmd = [mspaint, "/pt", file_path, printer_name]
            subprocess.run(cmd, check=True)

    else:
        raise RuntimeError(f"Printing not implemented for OS: {platform.system()}")


def main():
    parser = argparse.ArgumentParser(description="Compose template3 with 4 sample photos, duplicate side-by-side, preview, and optionally print.")
    parser.add_argument("--templates-dir", default="PhotoboothTemplates", help="Folder containing template3.png and template3.json")
    parser.add_argument("--template-name", default="template3", help="Base name (without extension). Uses <name>.png and <name>.json")
    parser.add_argument("--photos", nargs=4, default=["samplephoto1.jpg", "samplephoto2.jpg", "samplephoto3.jpg", "samplephoto4.jpg"],
                        help="Four photo filenames/paths (relative to script folder by default)")
    parser.add_argument("--gap", type=int, default=40, help="Gap in pixels between the two duplicated templates")
    parser.add_argument("--output", default="composed_side_by_side.jpg", help="Output file to save (jpg recommended for printing)")
    parser.add_argument("--preview", action="store_true", help="Preview the final image in your default viewer")
    parser.add_argument("--print", dest="do_print", action="store_true", help="Attempt to print the output")
    parser.add_argument("--printer", default="DNP QW410", help="Printer name as it appears on your OS")
    parser.add_argument("--copies", type=int, default=1, help="Number of copies to print (macOS supports directly; Windows loops)")

    args = parser.parse_args()

    base_dir = project_dir()
    templates_dir = os.path.join(base_dir, args.templates_dir)

    template_png = os.path.join(templates_dir, f"{args.template_name}.png")
    template_json = os.path.join(templates_dir, f"{args.template_name}.json")

    photo_paths = []
    for p in args.photos:
        # If user passes an absolute path, keep it. Otherwise resolve relative to script folder.
        photo_paths.append(p if os.path.isabs(p) else os.path.join(base_dir, p))

    # Validate files exist
    missing = [path for path in [template_png, template_json, *photo_paths] if not os.path.exists(path)]
    if missing:
        print("❌ Missing files:")
        for m in missing:
            print("  -", m)
        sys.exit(1)

    # Load template config + compose
    data = load_json(template_json)
    slots = parse_slots(data, template_json)

    composed = compose_into_template(template_png, slots, photo_paths)
    doubled = side_by_side(composed, gap=0)

    # Save output
    out_path = args.output if os.path.isabs(args.output) else os.path.join(base_dir, args.output)
    out_path = save_for_print(doubled, out_path)
    print(f"✅ Saved: {out_path}")

    # Preview
    if args.preview:
        preview(doubled)

    # Print
    if args.do_print:
        try:
            print(f"🖨️ Printing to '{args.printer}' ...")
            try_print(out_path, args.printer, copies=args.copies)
            print("✅ Print command sent.")
        except Exception as e:
            print("❌ Printing failed.")
            print("Reason:", e)
            print("\nTips:")
            print("- Confirm the printer name exactly matches your OS printer list.")
            print("- On macOS, run: lpstat -p -d  (to see printers)")
            print("- On Windows, check Control Panel → Devices and Printers for the exact name.")
            sys.exit(2)


if __name__ == "__main__":
    main()
