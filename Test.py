from pathlib import Path
import subprocess
from datetime import datetime
import time
from PIL import Image

INCOMING_DIR = Path(r"C:\photobooth\incoming")
INCOMING_DIR.mkdir(parents=True, exist_ok=True)


def to_wsl_path(path: Path) -> str:
    path = Path(path).resolve()
    drive = path.drive[0].lower()
    rest = str(path).replace("\\", "/")[2:]
    return f"/mnt/{drive}{rest}"


def capture_photo_gphoto(output_path: Path) -> Path:
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    wsl_output = to_wsl_path(output_path)

    cmd = [
        "wsl",
        "gphoto2",
        "--capture-image-and-download",
        "--filename",
        wsl_output,
    ]

    print("[CAMERA] Running:", " ".join(cmd))

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
        raise RuntimeError("Capture failed")

    if not output_path.exists():
        raise FileNotFoundError("File not found after capture")

    return output_path


# ----------------------------
# TEST RUN
# ----------------------------

print("📸 Starting capture test in 3 seconds...")
time.sleep(3)

ts = datetime.now().strftime("%Y%m%d_%H%M%S")
output = INCOMING_DIR / f"test_{ts}.jpg"

photo_path = capture_photo_gphoto(output)

print("✅ Photo captured:", photo_path)

# Show the image
img = Image.open(photo_path)
img.show()