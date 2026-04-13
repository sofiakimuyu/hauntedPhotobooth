from pathlib import Path
import sys
import subprocess

import numpy as np
from PIL import Image, ImageEnhance, ImageOps


def apply_vintage_bw(img: Image.Image) -> Image.Image:
    # Convert to grayscale
    bw = ImageOps.grayscale(img)

    # Increase contrast a bit
    bw = ImageEnhance.Contrast(bw).enhance(2.0)

    # Slight brightness lift
    bw = ImageEnhance.Brightness(bw).enhance(.9)

    # Back to RGB so we can tint and add grain
    bw_rgb = bw.convert("RGB")

    # Slight warm tone
    np_img = np.array(bw_rgb).astype(np.float32)
    np_img[..., 0] *= 1.2
    np_img[..., 1] *= 1.08
    np_img = np.clip(np_img, 0, 255)

   # shadow lift / matte blacks)
    np_img = np_img * 0.9 + 25

    # Add subtle grain
    noise = np.random.normal(0, 10, np_img.shape)
    np_img += noise
    np_img = np.clip(np_img, 0, 255).astype(np.uint8)

    return Image.fromarray(np_img)


def open_file(path: Path) -> None:
    if sys.platform.startswith("darwin"):
        subprocess.run(["open", str(path)], check=False)
    elif sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_vintage_bw.py path/to/photo.jpg")
        return

    input_path = Path(sys.argv[1]).expanduser().resolve()

    if not input_path.exists():
        print(f"File not found: {input_path}")
        return

    img = Image.open(input_path).convert("RGB")
    filtered = apply_vintage_bw(img)

    output_path = input_path.with_name(f"{input_path.stem}_vintage_bw{input_path.suffix}")
    filtered.save(output_path)

    print(f"Saved filtered image to: {output_path}")
    filtered.show()


if __name__ == "__main__":
    import os
    main()