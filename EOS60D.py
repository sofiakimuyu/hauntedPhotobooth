
# Import all the libraries you need
from datetime import datetime, date, timezone, timedelta
import tkinter as tk
from tkinter import messagebox
import json
import stripe
from PIL import Image, ImageTk, ImageOps, ImageEnhance
from pathlib import Path
import os
import traceback
import platform
from telemetry import Telemetry
import glob #maybe delete
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
import time
import subprocess
import shutil
from tempfile import NamedTemporaryFile
from analytics import log_print
from dashboard import run_dashboard
import signal
import sys
import numpy as np
import threading

# where store camera photos on the PC
INCOMING_DIR = Path(r"C:\photoboothphotos\incoming")
INCOMING_DIR.mkdir(parents=True, exist_ok=True)

# Where the photobooth templates are stored
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "PhotoboothTemplates"

# No preview for this camera
HAS_PREVIEW = False


def _eos60d_telegram_credentials() -> tuple[Optional[str], Optional[str]]:
    """
    Token + chat id for telemetry Telegram. Priority:
    1) Environment: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (or TELEGRAM_TOKEN + TELEGRAM_CHAT_ID)
    2) Local file: telegram_secrets.json next to this script (gitignored). Keys: bot_token, chat_id
    """
    token = (
        os.environ.get("TELEGRAM_BOT_TOKEN")
        or os.environ.get("TELEGRAM_TOKEN")
        or ""
    ).strip()
    chat = (
        os.environ.get("TELEGRAM_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT")
        or ""
    ).strip()
    if token and chat:
        return token, chat

    secrets_path = BASE_DIR / "telegram_secrets.json"
    if not secrets_path.is_file():
        return (token or None), (chat or None)
    try:
        data = json.loads(secrets_path.read_text(encoding="utf-8"))
        token = (token or str(data.get("bot_token", "") or "").strip()) or None
        chat = (chat or str(data.get("chat_id", "") or "").strip()) or None
        return token, chat
    except Exception as e:
        print(f"[WARN] Could not read telegram_secrets.json: {e}")
        return (token or None), (chat or None)


_tg_token, _tg_chat = _eos60d_telegram_credentials()

# Telemetry setup — Telegram optional unless token + chat id are configured (env or telegram_secrets.json)
telemetry = Telemetry(
    booth_id="Stanford booth",
    telegram_token=_tg_token,
    telegram_chat_id=_tg_chat,
    roll_warn_1=143,
    roll_warn_2=150,
    heartbeat_expected_interval_sec=3600,
    heartbeat_missed_multiplier=1.25,
)

if not _tg_token or not _tg_chat:
    print(
        "[Telemetry] Telegram disabled: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, "
        "or add telegram_secrets.json (see telegram_secrets.example.json)."
    )

telemetry.install_crash_hook()
START_TIME = time.time()
telemetry.booth_started()

# Stripe Setup
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
STRIPE_READER_ID = os.environ["STRIPE_READER_ID"]

# defines the data structures for photobooth templates
@dataclass
class Slot:
    id: int
    x: int
    y: int
    w: int
    h: int
    rotation: int = 0

@dataclass
class TemplateDef:
    key: str
    name: str
    png_path: str
    json_path: str
    slots: List[Slot]
    size: Tuple[int, int]
    layer_mode: str = "underlay"

# Define all the important functions we will need later

#Wait for a newly imported JPG to appear in INCOMING_DIR.  Returns the Path to the new file, or None if timed out.
def to_wsl_path(path: Path) -> str:
    """
    Convert a Windows path like:
        C:\\photobooth\\incoming\\img.jpg
    into a WSL path like:
        /mnt/c/photobooth/incoming/img.jpg
    """
    path = Path(path).resolve()
    drive = path.drive[0].lower()
    rest = str(path).replace("\\", "/")[2:]  # remove "C:"
    return f"/mnt/{drive}{rest}"


def capture_photo_gphoto(output_path: Path) -> Path:
    """
    Use gphoto2 through WSL to trigger the camera and download one image
    directly to output_path.

    This version downloads the captured image to the computer and then
    attempts to delete the just-captured file from the camera as well.
    """
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    wsl_output = to_wsl_path(output_path)

    # Step 1: capture and download to computer
    capture_cmd = [
        "wsl",
        "gphoto2",
        "--capture-image-and-download",
        "--filename",
        wsl_output,
    ]

    print("[CAMERA] Running:", " ".join(capture_cmd))
    result = subprocess.run(capture_cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(
            "gphoto2 capture failed.\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )

    if not output_path.exists():
        raise FileNotFoundError(
            f"Capture completed but file was not found: {output_path}"
        )

    # Step 2: best-effort delete from camera storage too
    # This may vary by camera model / gphoto2 driver support.
    delete_cmd = [
        "wsl",
        "gphoto2",
        "--delete-all-files",
        "--recurse",
    ]

    print("[CAMERA] Attempting camera cleanup:", " ".join(delete_cmd))
    delete_result = subprocess.run(delete_cmd, capture_output=True, text=True)

    if delete_result.returncode != 0:
        print(
            "[WARN] Photo downloaded, but delete-from-camera may have failed.\n"
            f"STDOUT:\n{delete_result.stdout}\n"
            f"STDERR:\n{delete_result.stderr}"
        )
    else:
        print("[CAMERA] Deleted files from camera storage.")

    return output_path


def capture_n_photos_gphoto(n: int = 4, pause_s: float = 0.8) -> list[Path]:
    """
    Capture n photos in sequence using gphoto2.
    Saves them into INCOMING_DIR and returns the file paths in order.
    """
    captured: list[Path] = []

    for i in range(n):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output_path = INCOMING_DIR / f"capture_{ts}_{i + 1}.jpg"

        print(f"[CAMERA] Capturing photo {i + 1}/{n}...")
        photo_path = capture_photo_gphoto(output_path)
        print(f"[CAMERA] Saved: {photo_path}")

        captured.append(photo_path)

        if i < n - 1:
            time.sleep(pause_s)

    return captured


def delete_files(paths: list[Path]):
    """
    Delete files after use for privacy / cleanup.
    """
    for p in paths:
        try:
            p.unlink(missing_ok=True)
            print(f"[CLEANUP] Deleted {p}")
        except Exception as e:
            print(f"[CLEANUP ERROR] {p}: {e}")

def print_image_windows(file_path: str, printer_name: str | None = None):
    """
    Print an image directly to a Windows printer with no dialog.
    Uses the installed printer driver via GDI.

    Requires:
        pip install pywin32
    """
    from pathlib import Path
    import win32print
    import win32ui
    from PIL import Image, ImageWin

    file_path = str(Path(file_path).resolve())
    print(f"[PRINT] Attempting to print file: {file_path}")
    if not Path(file_path).exists():
        raise FileNotFoundError(f"Print file not found: {file_path}")

    if not printer_name:
        printer_name = win32print.GetDefaultPrinter()
        print(f"[PRINT] Using default printer: {printer_name}")
    else:
        print(f"[PRINT] Using specified printer: {printer_name}")
        # Try to validate the printer exists
        try:
            printers = [p[2] for p in win32print.EnumPrinters(2)]
            if printer_name not in printers:
                print(f"[PRINT] Warning: Printer '{printer_name}' not found in system printers: {printers}")
                default_printer = win32print.GetDefaultPrinter()
                print(f"[PRINT] Falling back to default printer: {default_printer}")
                printer_name = default_printer
        except Exception as e:
            print(f"[PRINT] Could not enumerate printers: {e}")
            # Continue with the specified printer name

    img = Image.open(file_path).convert("RGB")
    print(f"[PRINT] Image loaded, size: {img.size}")

    hdc = win32ui.CreateDC()
    hdc.CreatePrinterDC(printer_name)
    print(f"[PRINT] Created printer DC for: {printer_name}")

    try:
        printable_w = hdc.GetDeviceCaps(8)   # HORZRES
        printable_h = hdc.GetDeviceCaps(10)  # VERTRES
        print(f"[PRINT] Printer resolution: {printable_w}x{printable_h}")

        img_w, img_h = img.size
        scale = min(printable_w / img_w, printable_h / img_h)
        draw_w = int(img_w * scale)
        draw_h = int(img_h * scale)
        print(f"[PRINT] Scaled image to: {draw_w}x{draw_h}")

        x1 = (printable_w - draw_w) // 2
        y1 = (printable_h - draw_h) // 2
        x2 = x1 + draw_w
        y2 = y1 + draw_h

        print(f"[PRINT] Starting print job...")
        hdc.StartDoc(Path(file_path).name)
        hdc.StartPage()

        dib = ImageWin.Dib(img)
        dib.draw(hdc.GetHandleOutput(), (x1, y1, x2, y2))

        hdc.EndPage()
        hdc.EndDoc()
        print(f"[PRINT] Print job completed successfully")

    finally:
        hdc.DeleteDC()
        print(f"[PRINT] Cleaned up printer DC")

def print_pil_image(img: Image.Image, printer_name: str | None = None):
    """
    Print a PIL image using a temporary JPG file, then remove it.
    """
    with NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        temp_path = Path(tmp.name)

    try:
        img.convert("RGB").save(temp_path, format="JPEG", quality=95)
        print_image_windows(str(temp_path), printer_name)
        print(f"[PRINT] Sent to printer: {temp_path}")

    except Exception as e:
        print("[PRINT ERROR]", e)
        raise

    finally:
        try:
            temp_path.unlink(missing_ok=True)
            print(f"[CLEANUP] Deleted temp print file {temp_path}")
        except Exception as e:
            print(f"[WARN] Could not delete temp print file {temp_path}: {e}")


def _pacific_today() -> date:
    """
    Today's date in US Pacific time, without depending on the system tz
    database. Prefers zoneinfo (correct DST year-round) but falls back to a
    fixed UTC-7 offset when the `tzdata` package / OS tz data is missing
    (common on Windows), so the booth never crashes at startup. UTC-7 (PDT)
    is correct for the Sept 22 promo window; the fallback only shifts the
    midnight boundary slightly on non-promo days, which does not affect price.
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Los_Angeles")).date()
    except Exception:
        return datetime.now(timezone(timedelta(hours=-7))).date()


def load_templates(dir_path) -> list[TemplateDef]:
    """
    Load all matching .png + .json template pairs from a folder.
    """
    dir_path = Path(dir_path)

    if not dir_path.is_dir():
        raise FileNotFoundError(f"Missing folder: {dir_path}")

    pngs = {p.stem: p for p in dir_path.glob("*.png")}
    jsons = {p.stem: p for p in dir_path.glob("*.json")}

    keys = sorted(set(pngs.keys()) & set(jsons.keys()))
    if not keys:
        raise FileNotFoundError(
            f"No templates found in {dir_path}\n"
            "Expected matching .png + .json pairs"
        )

    templates: list[TemplateDef] = []

    for key in keys:
        try:
            print("Loading template:", key)

            png_path = pngs[key]
            json_path = jsons[key]

            with json_path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            name = data.get("name", key)
            layer_mode = str(data.get("layer_mode", "") or "").strip().lower()
            slots_raw = data.get("slots") or []

            if len(slots_raw) != 4:
                raise ValueError("Template must define exactly 4 slots")

            defaults = data.get("defaults", {})
            slots: list[Slot] = []

            for s in slots_raw:
                merged = {**defaults, **s}

                w = int(merged["w"])
                h = int(merged["h"])

                if "cx" in merged and "cy" in merged:
                    x = int(round(float(merged["cx"]) - w / 2))
                    y = int(round(float(merged["cy"]) - h / 2))
                elif "x" in merged and "y" in merged:
                    x = int(merged["x"])
                    y = int(merged["y"])
                else:
                    raise ValueError(
                        f"Slot {merged.get('id', '?')} must define either "
                        f"(x, y) or (cx, cy)"
                    )

                slots.append(
                    Slot(
                        id=int(merged["id"]),
                        x=x,
                        y=y,
                        w=w,
                        h=h,
                        rotation=int(merged.get("rotation", 0)),
                    )
                )

            with Image.open(png_path) as im:
                w, h = im.size

            templates.append(
                TemplateDef(
                    key=key,
                    name=name,
                    png_path=str(png_path),
                    json_path=str(json_path),
                    slots=slots,
                    size=(w, h),
                    layer_mode=layer_mode,
                )
            )

        except Exception as e:
            raise RuntimeError(
                f"Template '{key}' failed to load ({json_path}): {e}"
            ) from e

    print("Loaded templates:", len(templates))
    return templates


def cover_resize(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """
    Resize to fully cover the target box, then center crop.
    No stretching.
    """
    iw, ih = img.size
    scale = max(target_w / iw, target_h / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    img = img.resize((nw, nh), Image.LANCZOS)

    left = (nw - target_w) // 2
    top = (nh - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def apply_bw_filter(img: Image.Image) -> Image.Image:
    """
    Apply black and white filter with specific adjustments.
    """
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


def compose(
    template_png_path: str,
    slots: List[Slot],
    photo_paths: List[str],
    filter_choice: str = "color",
    layer_mode: str = "underlay",
) -> Image.Image:
    """
    Fill a template background with the 4 captured photos in slot order.
    Returns one final composed PIL image.
    """
    template_path = Path(template_png_path)

    with Image.open(template_path) as im:
        template_img = im.convert("RGBA")

    if len(photo_paths) != 4:
        raise ValueError(f"Expected exactly 4 photo paths, got {len(photo_paths)}.")

    slots_sorted = sorted(slots, key=lambda s: s.id)
    if len(slots_sorted) != 4:
        raise ValueError(
            f"Template must define exactly 4 slots, got {len(slots_sorted)}."
        )

    is_overlay = str(layer_mode or "").strip().lower() == "overlay"
    if is_overlay:
        base = Image.new("RGBA", template_img.size, (255, 255, 255, 0))
    else:
        base = template_img.copy()

    for slot, photo_path in zip(slots_sorted, photo_paths):
        photo_path = Path(photo_path)

        if not photo_path.is_absolute():
            photo_path = BASE_DIR / photo_path

        with Image.open(photo_path) as im:
            img = im.convert("RGBA")

        # Apply black and white filter if selected
        if filter_choice == "bw":
            img = apply_bw_filter(img)

        if slot.rotation:
            img = img.rotate(slot.rotation, expand=True)

        fitted = cover_resize(img, slot.w, slot.h).convert("RGBA")
        mask = fitted.getchannel("A")
        base.paste(fitted, (slot.x, slot.y), mask)

    if is_overlay:
        base.alpha_composite(template_img)

    return base


def side_by_side(
    img: Image.Image,
    gap: int = 0,
    bg: Tuple[int, int, int, int] = (255, 255, 255, 255),
) -> Image.Image:
    """
    Duplicate a strip side by side for perforated photo paper printing.
    """
    w, h = img.size
    canvas = Image.new("RGBA", (w * 2 + gap, h), bg)
    canvas.paste(img, (0, 0), img if img.mode == "RGBA" else None)
    canvas.paste(img, (w + gap, 0), img if img.mode == "RGBA" else None)
    return canvas

class PhotoBoothUI:
    def __init__(self, root: tk.Tk, telemetry):
        # ---- window setup ----
        self.root = root
        self.telemetry = telemetry
        self.root.title("PhotoBooth")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="black")
        self.root.bind("<Escape>", lambda e: self.cleanup_and_exit())

        # ---- templates ----
        self.templates = []

        # ---- state ----
        self.is_running_sequence = False
        self.photo_index = 1
        self.total_photos = 4
        self.filter_choice = "color"  # "color" or "bw"
        # ---- pricing ----
        # One-day $1 promo: on Sept 22, 2026 (Pacific time) the booth charges
        # $1.00; every other day it charges the regular price. Anchored to
        # America/Los_Angeles so it flips at Pacific midnight regardless of the
        # machine's system timezone, and auto-reverts the next day.
        REGULAR_PRICE_CENTS = 424
        PROMO_PRICE_CENTS = 100
        PROMO_DAY = date(2026, 9, 22)
        today_pacific = _pacific_today()
        self.session_price_cents = (
            PROMO_PRICE_CENTS if today_pacific == PROMO_DAY else REGULAR_PRICE_CENTS
        )
        self.approved_access_codes = {
            "i'm a person in a high place",
            "sorry",
            "i want to make it up to you",
            "sorry it failed before",
            "i love you blair",
            "free for you!",
            "i love you",
            "I love you Sathvi",
            "debugwarrior"
        }
        self.one_time_access_codes = {
            "strikeapose",
            "saycheez",
            "flashme",
            "snapit",
            "boothie",
            "oopsie",
            "strip",
            "smile",
            "freeslay",
            "slay",
        }
        self.used_codes_path = BASE_DIR / "used_access_codes.json"
        self.used_one_time_access_codes = self._load_used_one_time_access_codes()

        # ---- capture / printing ----
        self.captured_files = []  # stores captured image paths
        self.templates_dir = Path(__file__).resolve().parent / "PhotoboothTemplates"

        self.printer_name = "DNPQW410"  # Set to your specific printer name
        self.last_composed_strip = None

        # ---- assets ----
        self.welcome_bg_path = os.path.join(
            os.path.dirname(__file__),
            "stanfordwelcomebackground.png"
        )
        self.code_bg_path = os.path.join(
            os.path.dirname(__file__),
            "specialpersonbackground.png"
        )

        print(
            "WELCOME BG PATH:",
            self.welcome_bg_path,
            "exists?",
            os.path.exists(self.welcome_bg_path)
        )

        self.tk_welcome_bg = None
        self.tk_code_bg = None

        self._reader_should_run = False
        self._reader_generation = 0
        self._reader_payment_intent_id = None
        self._code_screen_active = False

        # ---- fonts ----
        self.font_title = ("Helvetica", 64, "bold")
        self.font_subtitle = ("Helvetica", 28, "normal")
        self.font_button = ("Helvetica", 28, "bold")
        self.font_small = ("Helvetica", 24, "bold")
        self.font_countdown = ("Helvetica", 160, "bold")

        # ---- main container ----
        self.container = tk.Frame(self.root, bg="black")
        self.container.pack(expand=True, fill="both")

        # ---- build screens ----
        self.build_welcome_screen()
        self.build_code_entry_screen()
        self.build_capture_screen()
        self.build_filter_screen()
        self.build_template_picker_screen()

        # Staff: after loading a new paper roll (telemetry roll limit was hit)
        self.root.bind("<Control-Shift-P>", self._operator_clear_paper_lock)
        self.root.bind("<Control-Shift-p>", self._operator_clear_paper_lock)
        self.root.report_callback_exception = self._handle_tk_exception

        # ---- start ----
        self.show_welcome_screen()

    def _operator_clear_paper_lock(self, event=None):
        try:
            self.telemetry.clear_out_of_paper_lock()
        except Exception as e:
            print(f"[WARN] clear_out_of_paper_lock: {e}")
            return "break"
        if getattr(self, "_is_shutting_down", False):
            return "break"
        self.show_welcome_screen()
        return "break"

    def _handle_tk_exception(self, exc_type, exc_value, exc_traceback):
        print("[TK ERROR] Unhandled UI exception:")
        traceback.print_exception(exc_type, exc_value, exc_traceback)
        try:
            self.is_running_sequence = False
            self.session_paid = False
            self.show_welcome_screen()
        except Exception as recovery_error:
            print(f"[WARN] Could not recover from UI exception: {recovery_error}")

    def _formatted_session_price(self) -> str:
        return f"${self.session_price_cents / 100:.2f}"

    def _default_welcome_subtitle(self) -> str:
        return f"Pay {self._formatted_session_price()} on the card reader to start"

    def _set_welcome_subtitle(self, text: str):
        try:
            self.welcome_canvas.itemconfig(
                self.subtitle_item,
                text=text,
                fill="white",
                font=self.font_subtitle,
            )
        except Exception:
            pass

    def _is_reader_generation_current(self, generation: int) -> bool:
        return generation == getattr(self, "_reader_generation", -1)

    def _cancel_reader_action_safely(self):
        try:
            stripe.terminal.Reader.cancel_action(STRIPE_READER_ID)
        except Exception:
            pass

    def _clear_reader_payment_intent(self):
        self._reader_payment_intent_id = None

    def _get_reader_payment_intent_status(self, payment_intent_id: str | None = None):
        target_id = payment_intent_id or getattr(self, "_reader_payment_intent_id", None)
        if not target_id:
            return None

        try:
            pi = stripe.PaymentIntent.retrieve(target_id)
            status = getattr(pi, "status", None)
            print(f"[STRIPE] PaymentIntent {target_id} status is {status}")
            return status
        except Exception as e:
            print(f"[STRIPE] Could not retrieve PaymentIntent {target_id}: {e}")
            return None

    def _is_reader_busy_error(self, error: Exception) -> bool:
        code = getattr(error, "code", None)
        if code == "terminal_reader_busy":
            return True
        return "reader is currently busy processing another request" in str(error).lower()

    def _stripe_obj_get(self, obj, attr, default=None):
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(attr, default)
        return getattr(obj, attr, default)

    def _extract_payment_intent_id_from_reader_action(self, action):
        if not action:
            return None

        # Stripe terminal actions can nest the PaymentIntent id under the
        # action-specific payload. Try the common shapes first, then fall back
        # to a small recursive search.
        for action_attr in (
            "process_payment_intent",
            "collect_payment_method",
            "confirm_payment_intent",
        ):
            payload = self._stripe_obj_get(action, action_attr)
            payment_intent_id = self._stripe_obj_get(payload, "payment_intent")
            if payment_intent_id:
                return payment_intent_id

        direct_payment_intent_id = self._stripe_obj_get(action, "payment_intent")
        if direct_payment_intent_id:
            return direct_payment_intent_id

        def _search(node, depth=0):
            if node is None or depth > 3:
                return None
            if isinstance(node, str):
                return node if node.startswith("pi_") else None
            if isinstance(node, dict):
                payment_intent_id = node.get("payment_intent")
                if isinstance(payment_intent_id, str) and payment_intent_id.startswith("pi_"):
                    return payment_intent_id
                for value in node.values():
                    found = _search(value, depth + 1)
                    if found:
                        return found
                return None
            if isinstance(node, (list, tuple)):
                for value in node:
                    found = _search(value, depth + 1)
                    if found:
                        return found
                return None

            payment_intent_id = getattr(node, "payment_intent", None)
            if isinstance(payment_intent_id, str) and payment_intent_id.startswith("pi_"):
                return payment_intent_id

            for attr in ("collect_payment_method", "process_payment_intent", "confirm_payment_intent"):
                found = _search(getattr(node, attr, None), depth + 1)
                if found:
                    return found
            return None

        return _search(action)

    def _get_reader_action_details(self):
        try:
            reader = stripe.terminal.Reader.retrieve(STRIPE_READER_ID)
            action = getattr(reader, "action", None)
            status = getattr(action, "status", None) if action else None
            payment_intent_id = self._extract_payment_intent_id_from_reader_action(action)
            if status:
                print(f"[STRIPE] Reader currently has action status {status}")
            if payment_intent_id:
                print(f"[STRIPE] Reader action is tied to PaymentIntent {payment_intent_id}")
            return status, payment_intent_id
        except Exception as e:
            print(f"[STRIPE] Could not retrieve reader action: {e}")
            return None, None

    def _get_reader_action_status(self):
        status, _ = self._get_reader_action_details()
        return status

    def _wait_for_reader_idle(self, timeout_sec: float = 20.0, stable_idle_sec: float = 2.0):
        deadline = time.time() + timeout_sec
        idle_since = None
        while time.time() < deadline:
            status = self._get_reader_action_status()
            if not status:
                if idle_since is None:
                    idle_since = time.time()
                    print("[STRIPE] Reader appears idle; waiting briefly to confirm.")
                elif time.time() - idle_since >= stable_idle_sec:
                    print("[STRIPE] Reader confirmed idle.")
                    return True
                time.sleep(0.25)
                continue
            idle_since = None
            if status in ("failed", "canceled", "succeeded"):
                print(f"[STRIPE] Reader action is terminal ({status}); waiting for it to clear.")
                time.sleep(0.5)
                continue
            time.sleep(0.5)
        print("[STRIPE] Reader did not become idle before timeout.")
        return False

    def _cancel_payment_intent_if_reusable(self, payment_intent_id: str | None):
        if not payment_intent_id:
            return
        try:
            pi = stripe.PaymentIntent.retrieve(payment_intent_id)
            pi_status = getattr(pi, "status", None)
            if pi_status in ("requires_payment_method", "requires_confirmation"):
                stripe.PaymentIntent.cancel(payment_intent_id)
                print(f"[STRIPE] Canceled unused PaymentIntent {payment_intent_id}")
        except Exception as e:
            print(f"[STRIPE] Could not cancel PaymentIntent {payment_intent_id}: {e}")

    def _reset_reader_for_new_payment(self):
        self._cancel_payment_intent_if_reusable(getattr(self, "_reader_payment_intent_id", None))
        print("[STRIPE] Resetting reader state before creating a new PaymentIntent.")
        self._cancel_reader_action_safely()
        self._wait_for_reader_idle()
        self._clear_reader_payment_intent()

    def _prepare_reader_for_payment(self):
        self._reset_reader_for_new_payment()

    def _finish_reader_wait_with_payment_status(
        self,
        generation: int,
        payment_intent_id: str,
        fallback_status: str,
        *,
        allow_requires_payment_method_failure: bool,
    ):
        pi_status = self._get_reader_payment_intent_status(payment_intent_id)
        resolved_status = pi_status or fallback_status

        if resolved_status == "succeeded":
            print(f"[STRIPE] PaymentIntent {payment_intent_id} succeeded; starting booth.")
            self.root.after(
                0,
                lambda gen=generation: self._on_reader_payment_result(gen, True, "succeeded")
            )
            return True

        if resolved_status in ("failed", "canceled") or (
            allow_requires_payment_method_failure and resolved_status == "requires_payment_method"
        ):
            print(f"[STRIPE] PaymentIntent {payment_intent_id} ended with status {resolved_status}.")
            self.root.after(
                0,
                lambda gen=generation, status=resolved_status:
                self._on_reader_payment_result(gen, False, status)
            )
            return True

        return False

    def _poll_reader_action_until_terminal(self, generation: int, payment_intent_id: str):
        last_logged_status = None
        saw_live_action = False
        while self._reader_should_run and self._is_reader_generation_current(generation):
            reader = stripe.terminal.Reader.retrieve(STRIPE_READER_ID)
            action = getattr(reader, "action", None)
            current_payment_intent_id = (
                self._extract_payment_intent_id_from_reader_action(action)
                or payment_intent_id
            )
            self._reader_payment_intent_id = current_payment_intent_id

            if not action:
                pi_status = self._get_reader_payment_intent_status(current_payment_intent_id)
                if pi_status == "succeeded":
                    print(f"[STRIPE] PaymentIntent {current_payment_intent_id} succeeded without reader action; starting booth.")
                    self.root.after(
                        0,
                        lambda gen=generation: self._on_reader_payment_result(gen, True, "succeeded")
                    )
                    return True
                if saw_live_action and pi_status in ("requires_payment_method", "requires_confirmation", "failed", "canceled"):
                    print(
                        f"[STRIPE] Reader action cleared and PaymentIntent {current_payment_intent_id} resolved to {pi_status}; restarting payment."
                    )
                    self.root.after(
                        0,
                        lambda gen=generation, status=pi_status:
                        self._on_reader_payment_result(gen, False, status)
                    )
                    return True
                time.sleep(0.5)
                continue

            status = getattr(action, "status", None)
            if status and status != last_logged_status:
                print(f"[STRIPE] Reader live status is {status}")
                last_logged_status = status
            if status:
                saw_live_action = True

            if status == "succeeded":
                return self._finish_reader_wait_with_payment_status(
                    generation,
                    current_payment_intent_id,
                    "succeeded",
                    allow_requires_payment_method_failure=False,
                )

            if status == "failed":
                return self._finish_reader_wait_with_payment_status(
                    generation,
                    current_payment_intent_id,
                    "failed",
                    allow_requires_payment_method_failure=True,
                )

            if status == "canceled":
                print("[STRIPE] Reader action canceled.")
                self.root.after(
                    0,
                    lambda gen=generation: self._on_reader_payment_result(gen, False, "canceled")
                )
                return True

            time.sleep(0.5)

        return False

    def _stop_reader_waiting(self, *, cancel_reader: bool, clear_payment_intent: bool = False):
        self._reader_should_run = False
        self._payment_in_progress = False
        if clear_payment_intent:
            self._clear_reader_payment_intent()
        self._reader_generation += 1
        if cancel_reader:
            threading.Thread(
                target=self._cancel_reader_action_safely,
                daemon=True
            ).start()

    def _start_reader_waiting(self):
        if getattr(self, "_is_shutting_down", False):
            return
        if self.telemetry.is_out_of_paper():
            return
        if self.session_paid or self.is_running_sequence:
            return
        if getattr(self, "_code_screen_active", False):
            return
        if getattr(self, "_reader_should_run", False):
            return

        self._reader_should_run = True
        self._payment_in_progress = True
        self._reader_generation += 1
        generation = self._reader_generation
        self._set_welcome_subtitle(self._default_welcome_subtitle())

        threading.Thread(
            target=lambda: self._reader_wait_loop(generation),
            daemon=True
        ).start()

    def _create_reader_payment_intent(self):
        pi = stripe.PaymentIntent.create(
            amount=self.session_price_cents,
            currency="usd",
            payment_method_types=["card_present"],
            description="Photobooth session"
        )
        print(f"[STRIPE] Created new PaymentIntent {pi.id}")
        self._reader_payment_intent_id = pi.id
        return pi

    def _reader_wait_loop(self, generation: int):
        pi = None
        try:
            self._prepare_reader_for_payment()
            if not self._is_reader_generation_current(generation):
                return

            pi = self._create_reader_payment_intent()
            if not self._is_reader_generation_current(generation):
                return

            pi_status = getattr(pi, "status", None)
            if pi_status == "succeeded":
                print(f"[STRIPE] PaymentIntent {pi.id} already succeeded; starting booth.")
                self.root.after(
                    0,
                    lambda gen=generation: self._on_reader_payment_result(gen, True, "succeeded")
                )
                return

            try:
                print(f"[STRIPE] Sending PaymentIntent {pi.id} to reader {STRIPE_READER_ID}")
                stripe.terminal.Reader.process_payment_intent(
                    STRIPE_READER_ID,
                    payment_intent=pi.id
                )
                print(f"[STRIPE] Reader accepted PaymentIntent {pi.id}")
            except Exception as e:
                if self._is_reader_busy_error(e):
                    print("[STRIPE] Reader is still busy after reset; canceling this PaymentIntent and retrying.")
                    self._cancel_payment_intent_if_reusable(pi.id)
                    self._reset_reader_for_new_payment()
                    self.root.after(
                        1500,
                        lambda gen=generation: self._on_reader_payment_error(
                            gen,
                            "Reader was busy; it was reset and will retry with a fresh payment."
                        )
                    )
                    return
                else:
                    raise

            self._poll_reader_action_until_terminal(
                generation,
                getattr(self, "_reader_payment_intent_id", None) or pi.id
            )
            return

        except Exception as e:
            pi_status = self._get_reader_payment_intent_status(getattr(pi, "id", None))
            if pi_status == "succeeded":
                print("[STRIPE] Reader command errored, but payment succeeded; starting booth.")
                self.root.after(
                    0,
                    lambda gen=generation: self._on_reader_payment_result(gen, True, "succeeded")
                )
                return
            self.root.after(
                0,
                lambda gen=generation, msg=str(e): self._on_reader_payment_error(gen, msg)
            )

    def _on_reader_payment_result(self, generation: int, ok: bool, status: str):
        if not self._is_reader_generation_current(generation):
            return

        self._reader_should_run = False
        self._payment_in_progress = False
        self._reader_generation += 1

        if ok:
            self.session_paid = True
            self._welcome_tap_enabled = False
            self._set_welcome_subtitle("Paid. Starting...")
            self.root.after(100, self.start_sequence)
            return

        self.session_paid = False
        self._set_welcome_subtitle(self._default_welcome_subtitle())
        self.root.after(250, self._start_reader_waiting)

    def _on_reader_payment_error(self, generation: int, message: str):
        if not self._is_reader_generation_current(generation):
            return

        print(f"[STRIPE] Reader wait error: {message}")
        self._reader_should_run = False
        self._payment_in_progress = False
        self._reader_generation += 1
        self._set_welcome_subtitle(self._default_welcome_subtitle())
        self.root.after(1000, self._start_reader_waiting)

    def build_welcome_screen(self):
        self.welcome_frame = tk.Frame(self.container, bg="black")

        self.welcome_canvas = tk.Canvas(
            self.welcome_frame,
            bg="black",
            highlightthickness=0,
            borderwidth=0
        )
        self.welcome_canvas.pack(expand=True, fill="both")

        # Optional background image
        self.tk_welcome_bg = None
        if self.welcome_bg_path:
            try:
                img = Image.open(self.welcome_bg_path)
                img = img.resize(
                    (self.root.winfo_screenwidth(), self.root.winfo_screenheight()),
                    Image.LANCZOS
                )
                self.tk_welcome_bg = ImageTk.PhotoImage(img)
            except Exception as e:
                print(f"[WARN] Could not load welcome background: {e}")
                self.tk_welcome_bg = None

        self.welcome_bg_item = self.welcome_canvas.create_image(
            0,
            0,
            anchor="nw",
            image=self.tk_welcome_bg
        )
        self.welcome_canvas.tag_lower(self.welcome_bg_item)

        # Title
        self.welcome_text_item = self.welcome_canvas.create_text(
            self.root.winfo_screenwidth() // 2,
            200,
            #pause on a message for now
            text="",
            fill="white",
            font=self.font_title,
            anchor="center"
        )

        # Subtitle
        self.subtitle_item = self.welcome_canvas.create_text(
            self.root.winfo_screenwidth() // 2,
            500,
            text=self._default_welcome_subtitle(),
            fill="white",
            font=self.font_subtitle,
            anchor="center"
        )

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        self.code_button_rect = self.welcome_canvas.create_rectangle(
            screen_w - 360, screen_h - 180, screen_w - 140, screen_h - 116,
            fill="black",
            outline="white",
            width=2
        )
        self.code_button_text = self.welcome_canvas.create_text(
            screen_w - 250,
            screen_h - 148,
            text="I have a code",
            fill="white",
            font=("Helvetica", 18, "bold"),
            anchor="center"
        )

        # Welcome/payment state
        self.session_paid = False
        self._payment_in_progress = False
        self._welcome_tap_enabled = True

        def _on_code_button_tap(event=None):
            self.show_code_entry_screen()
            return "break"

        def _tap_wrapper(event=None):
            bbox = self.welcome_canvas.bbox(self.code_button_rect)
            if bbox and bbox[0] <= event.x <= bbox[2] and bbox[1] <= event.y <= bbox[3]:
                return _on_code_button_tap(event)
            return "break"

        # Bind tap/click across the whole welcome screen
        self.welcome_canvas.bind("<Button-1>", _tap_wrapper)
        self.welcome_canvas.tag_bind(self.welcome_bg_item, "<Button-1>", _tap_wrapper)
        self.welcome_canvas.tag_bind(self.welcome_text_item, "<Button-1>", _tap_wrapper)
        self.welcome_canvas.tag_bind(self.subtitle_item, "<Button-1>", _tap_wrapper)
        self.welcome_canvas.tag_bind(self.code_button_rect, "<Button-1>", _on_code_button_tap)
        self.welcome_canvas.tag_bind(self.code_button_text, "<Button-1>", _on_code_button_tap)

    def build_code_entry_screen(self):
        self.code_frame = tk.Frame(self.container, bg="black")

        self.code_bg_label = tk.Label(self.code_frame, bg="black", borderwidth=0)
        self.code_bg_label.place(x=0, y=0, relwidth=1, relheight=1)
        self._load_code_background()

        self.code_panel = tk.Frame(
            self.code_frame,
            bg="black",
            highlightthickness=2,
            highlightbackground="white",
            bd=0,
        )
        self.code_panel.place(
            relx=0.28,
            rely=0.5,
            anchor="center",
            width=660,
            height=620,
        )

        self.code_close_button = tk.Button(
            self.code_panel,
            text="X",
            command=self.show_welcome_screen,
            font=("Helvetica", 20, "bold"),
            bg="black",
            fg="white",
            activebackground="black",
            activeforeground="white",
            borderwidth=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.code_close_button.place(relx=1.0, x=-28, y=24, anchor="ne")

        self.code_title_label = tk.Label(
            self.code_panel,
            text="Enter your code",
            bg="black",
            fg="white",
            font=("Helvetica", 44, "bold"),
        )
        self.code_title_label.pack(pady=(36, 16))

        self.code_error_var = tk.StringVar(value="")
        self.code_entry_var = tk.StringVar()

        self.code_entry = tk.Entry(
            self.code_panel,
            textvariable=self.code_entry_var,
            font=("Helvetica", 26),
            justify="center",
            width=28,
            relief="flat",
            highlightthickness=3,
            highlightbackground="white",
            highlightcolor="white",
            bd=0,
        )
        self.code_entry.pack(pady=(0, 14), ipadx=12, ipady=18)
        self.code_entry.bind("<Return>", self._submit_access_code)
        self.code_entry.bind("<Button-1>", self._focus_code_entry)
        self.code_entry.bind("<FocusIn>", self._focus_code_entry)

        self.code_enter_button = tk.Button(
            self.code_panel,
            text="Enter",
            command=self._submit_access_code,
            font=("Helvetica", 24, "bold"),
            bg="white",
            fg="black",
            activebackground="#f0f0f0",
            activeforeground="black",
            relief="flat",
            padx=28,
            pady=14,
            cursor="hand2",
        )
        self.code_enter_button.pack(pady=(0, 0))

        self.code_error_label = tk.Label(
            self.code_panel,
            textvariable=self.code_error_var,
            bg="black",
            fg="#ff6b6b",
            font=("Helvetica", 22, "bold"),
        )
        self.code_error_label.pack(pady=(0, 6))

        self.code_keyboard_frame = tk.Frame(self.code_panel, bg="black")
        self.code_keyboard_frame.pack(pady=(0, 0))
        self.code_keyboard_visible = False
        self._build_code_keyboard()
        self.code_keyboard_frame.pack_forget()

    def _load_code_background(self):
        if not self.code_bg_path:
            return

        try:
            img = Image.open(self.code_bg_path)
            img = img.resize(
                (self.root.winfo_screenwidth(), self.root.winfo_screenheight()),
                Image.LANCZOS
            )
            self.tk_code_bg = ImageTk.PhotoImage(img)
            self.code_bg_label.configure(image=self.tk_code_bg)
        except Exception as e:
            print(f"[WARN] Could not load code background: {e}")
            self.tk_code_bg = None
            self.code_bg_label.configure(image="", bg="black")

    def _focus_code_entry(self, event=None):
        try:
            self.code_entry.focus_set()
        except Exception:
            pass
        self._show_touch_keyboard()
        return None

    def _build_code_keyboard(self):
        rows = [
            list("QWERTYUIOP"),
            list("ASDFGHJKL"),
            list("ZXCVBNM"),
        ]

        for row_index, chars in enumerate(rows):
            row_frame = tk.Frame(self.code_keyboard_frame, bg="black")
            row_frame.pack(pady=4)
            for ch in chars:
                btn = tk.Button(
                    row_frame,
                    text=ch,
                    command=lambda value=ch: self._code_keyboard_press(value),
                    font=("Helvetica", 16, "bold"),
                    width=3,
                    bg="#1a1a1a",
                    fg="white",
                    activebackground="#2b2b2b",
                    activeforeground="white",
                    relief="flat",
                    padx=4,
                    pady=8,
                    cursor="hand2",
                )
                btn.pack(side="left", padx=3)

        action_row = tk.Frame(self.code_keyboard_frame, bg="black")
        action_row.pack(pady=(10, 0))

        buttons = [
            ("Space", lambda: self._code_keyboard_press(" "), 10),
            ("Back", self._code_keyboard_backspace, 6),
            ("Clear", self._code_keyboard_clear, 6),
            ("Enter", self._submit_access_code, 6),
        ]

        for label, cmd, width in buttons:
            btn = tk.Button(
                action_row,
                text=label,
                command=cmd,
                font=("Helvetica", 15, "bold"),
                width=width,
                bg="white" if label == "Enter" else "#1a1a1a",
                fg="black" if label == "Enter" else "white",
                activebackground="#f0f0f0" if label == "Enter" else "#2b2b2b",
                activeforeground="black" if label == "Enter" else "white",
                relief="flat",
                padx=6,
                pady=10,
                cursor="hand2",
            )
            btn.pack(side="left", padx=4)

    def _show_touch_keyboard(self):
        if not getattr(self, "code_keyboard_visible", False):
            self.code_keyboard_frame.pack(pady=(0, 0))
            self.code_keyboard_visible = True

    def _hide_touch_keyboard(self):
        if getattr(self, "code_keyboard_visible", False):
            self.code_keyboard_frame.pack_forget()
            self.code_keyboard_visible = False

    def _code_keyboard_press(self, value: str):
        current = self.code_entry_var.get()
        self.code_entry_var.set(current + value)
        self.code_entry.focus_set()
        self.code_entry.icursor(tk.END)
        self.code_entry.xview_moveto(1.0)

    def _code_keyboard_backspace(self):
        current = self.code_entry_var.get()
        self.code_entry_var.set(current[:-1])
        self.code_entry.focus_set()
        self.code_entry.icursor(tk.END)
        self.code_entry.xview_moveto(1.0)

    def _code_keyboard_clear(self):
        self.code_entry_var.set("")
        self.code_entry.focus_set()
        self.code_entry.icursor(0)
        self.code_entry.xview_moveto(0.0)

    def _load_used_one_time_access_codes(self) -> set[str]:
        if not self.used_codes_path.is_file():
            return set()
        try:
            data = json.loads(self.used_codes_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[WARN] Could not read used access codes: {e}")
            return set()

        if not isinstance(data, list):
            return set()
        return {
            str(code).strip().casefold()
            for code in data
            if str(code).strip()
        }

    def _save_used_one_time_access_codes(self):
        try:
            self.used_codes_path.write_text(
                json.dumps(sorted(self.used_one_time_access_codes), indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[WARN] Could not save used access codes: {e}")

    def _grant_code_access(self):
        self._cancel_code_screen_timeout()
        self.code_error_var.set("")
        self.code_entry_var.set("")
        self._hide_touch_keyboard()
        self._code_screen_active = False
        self.session_paid = True
        self._payment_in_progress = False
        self._welcome_tap_enabled = False
        self.start_sequence()
        return "break"

    def _submit_access_code(self, event=None):
        entered_code = (self.code_entry_var.get() or "").strip().casefold()
        if entered_code in self.one_time_access_codes:
            if entered_code in self.used_one_time_access_codes:
                self.code_error_var.set("the code has already been used")
                try:
                    self.code_entry.selection_range(0, tk.END)
                    self.code_entry.focus_set()
                except Exception:
                    pass
                self._show_touch_keyboard()
                return "break"

            self.used_one_time_access_codes.add(entered_code)
            self._save_used_one_time_access_codes()
            return self._grant_code_access()

        if entered_code in self.approved_access_codes:
            return self._grant_code_access()

        self.code_error_var.set("error code not recognized")
        try:
            self.code_entry.selection_range(0, tk.END)
            self.code_entry.focus_set()
        except Exception:
            pass
        self._show_touch_keyboard()
        return "break"

    def build_capture_screen(self):
        self.capture_frame = tk.Frame(self.container, bg="black")

        self.capture_canvas = tk.Canvas(
            self.capture_frame,
            bg="black",
            highlightthickness=0,
            borderwidth=0
        )
        self.capture_canvas.pack(expand=True, fill="both")

        # Splash: "4 photos, get ready!"
        self.splash_item = self.capture_canvas.create_text(
            self.root.winfo_screenwidth() // 2,
            self.root.winfo_screenheight() // 2,
            text="4 photos, get ready!",
            fill="white",
            font=self.font_title,
            anchor="center",
            state="hidden"
        )

        # Top-right "Photo X"
        self.photo_item = self.capture_canvas.create_text(
            self.root.winfo_screenwidth() - 40,
            30,
            text="Photo 1",
            fill="white",
            font=self.font_small,
            anchor="ne",
            state="hidden"
        )

        # Big countdown number
        self.countdown_item = self.capture_canvas.create_text(
            self.root.winfo_screenwidth() // 2,
            int(self.root.winfo_screenheight() * 0.52),
            text="",
            fill="white",
            font=self.font_countdown,
            anchor="center",
            state="hidden"
        )

    def build_filter_screen(self):
        self.filter_frame = tk.Frame(self.container, bg="black")

        self.filter_canvas = tk.Canvas(
            self.filter_frame,
            bg="black",
            highlightthickness=0,
            borderwidth=0
        )
        self.filter_canvas.pack(expand=True, fill="both")

        # Background image
        self.tk_filter_bg = None
        self.filter_bg_item = None

        # Bind canvas click to detect left/right tap
        self.filter_canvas.bind(
            "<Button-1>",
            self._on_filter_canvas_click
        )

    def _on_filter_canvas_click(self, event):
        """Detect left vs right click on filter screen."""
        screen_w = self.root.winfo_screenwidth()
        if event.x < screen_w // 2:
            # Left side: black & white
            self.on_filter_selected("bw")
        else:
            # Right side: color
            self.on_filter_selected("color")

    def _load_filter_background(self):
        """Load the stylebackground.png image."""
        filter_bg_path = os.path.join(
            os.path.dirname(__file__),
            "stylebackground.png"
        )
        if os.path.exists(filter_bg_path):
            try:
                img = Image.open(filter_bg_path)
                img = img.resize(
                    (self.root.winfo_screenwidth(), self.root.winfo_screenheight()),
                    Image.LANCZOS
                )
                self.tk_filter_bg = ImageTk.PhotoImage(img)
                if self.filter_bg_item:
                    self.filter_canvas.delete(self.filter_bg_item)
                self.filter_bg_item = self.filter_canvas.create_image(
                    0,
                    0,
                    anchor="nw",
                    image=self.tk_filter_bg
                )
                self.filter_canvas.tag_lower(self.filter_bg_item)
            except Exception as e:
                print(f"[WARN] Could not load filter background: {e}")
        else:
            print(f"[WARN] Filter background not found: {filter_bg_path}")

    # -------------------------
    # Screen navigation
    # -------------------------
    def clear_screens(self):
        self.stop_welcome_pulse()
        for child in self.container.winfo_children():
            child.pack_forget()

    def _cancel_code_screen_timeout(self):
        after_id = getattr(self, "_code_screen_timeout_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self._code_screen_timeout_id = None

    def show_code_entry_screen(self):
        self._code_screen_active = True
        self._stop_reader_waiting(cancel_reader=True, clear_payment_intent=False)
        self.clear_screens()
        self._load_code_background()
        self.code_frame.pack(expand=True, fill="both")
        self.code_entry_var.set("")
        self.code_error_var.set("")
        self.root.after(100, self._focus_code_entry)
        self._cancel_code_screen_timeout()
        self._code_screen_timeout_id = self.root.after(5 * 60 * 1000, self.show_welcome_screen)

    def show_welcome_screen(self):
        self._cancel_code_screen_timeout()
        self._code_screen_active = False
        self._stop_reader_waiting(
            cancel_reader=True,
            clear_payment_intent=not (self.session_paid or self.is_running_sequence),
        )
        self._hide_touch_keyboard()
        self.clear_screens()
        self.welcome_frame.pack(expand=True, fill="both")

        # Reset session/run state
        self.is_running_sequence = False
        self.session_paid = False
        self._payment_in_progress = False
        self.filter_choice = "color"
        self.code_entry_var.set("")
        self.code_error_var.set("")

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        if self.telemetry.is_out_of_paper():
            self._welcome_tap_enabled = False
            self.stop_welcome_pulse()
            try:
                self.welcome_canvas.coords(
                    self.welcome_text_item, sw // 2, int(sh * 0.40)
                )
                self.welcome_canvas.coords(
                    self.subtitle_item, sw // 2, int(sh * 0.58)
                )
                self.welcome_canvas.itemconfig(
                    self.welcome_text_item,
                    text="Out of Paper!",
                    fill="white",
                    font=self.font_title,
                )
                self.welcome_canvas.itemconfig(
                    self.subtitle_item,
                    text=(
                        "This booth cannot take payments right now.\n"
                        "Staff: load a new roll, then press Ctrl+Shift+P."
                    ),
                    fill="white",
                    font=self.font_subtitle,
                    width=min(920, sw - 80),
                )
            except Exception:
                pass
            return

        self._welcome_tap_enabled = True
        try:
            self.welcome_canvas.coords(self.welcome_text_item, sw // 2, 200)
            self.welcome_canvas.coords(self.subtitle_item, sw // 2, 500)
            self.welcome_canvas.itemconfig(
                self.welcome_text_item,
                text="",
                fill="white",
                font=self.font_title,
            )
            self.welcome_canvas.itemconfig(
                self.subtitle_item,
                text=self._default_welcome_subtitle(),
                fill="white",
                font=self.font_subtitle,
                width=0,
            )
        except Exception:
            pass

        self._welcome_subtitle_base_y = None
        try:
            self.start_welcome_pulse()
        except Exception:
            pass
        self.root.after(100, self._start_reader_waiting)

    def _get_font_with_size(self, base_font, size: int):
        """
        Return a font tuple with the same family/style as base_font
        but using a different size.
        """
        if isinstance(base_font, tuple) and len(base_font) >= 2:
            family = base_font[0]
            rest = list(base_font[2:])
            return (family, size, *rest)
        return base_font

    def start_welcome_pulse(self):
        """Start a subtle pulsing animation on the welcome subtitle."""
        self.stop_welcome_pulse()
        self._welcome_pulse_on = True

        self._welcome_subtitle_base_y = getattr(self, "_welcome_subtitle_base_y", None)
        if self._welcome_subtitle_base_y is None:
            coords = self.welcome_canvas.coords(self.subtitle_item)
            if coords and len(coords) >= 2:
                self._welcome_subtitle_base_y = coords[1]
            else:
                self._welcome_subtitle_base_y = 300

        base_size = 0
        if isinstance(self.font_subtitle, tuple) and len(self.font_subtitle) >= 2:
            base_size = int(self.font_subtitle[1])
        if base_size <= 0:
            base_size = 32

        self._welcome_pulse_state = 0

        def _tick():
            if not getattr(self, "_welcome_pulse_on", False):
                return

            cycle = self._welcome_pulse_state % 4

            fill = "white" if cycle in (0, 2) else "#d9d9d9"

            bob = 0
            if cycle == 1:
                bob = -3
            elif cycle == 3:
                bob = 3

            size = base_size if cycle in (0, 2) else (base_size + 1)
            font = self._get_font_with_size(self.font_subtitle, size)

            try:
                self.welcome_canvas.itemconfig(
                    self.subtitle_item,
                    fill=fill,
                    font=font
                )

                x, _ = self.welcome_canvas.coords(self.subtitle_item)
                self.welcome_canvas.coords(
                    self.subtitle_item,
                    x,
                    self._welcome_subtitle_base_y + bob
                )
            except Exception:
                self.stop_welcome_pulse()
                return

            self._welcome_pulse_state += 1
            self._welcome_pulse_after_id = self.root.after(450, _tick)

        self._welcome_pulse_after_id = self.root.after(0, _tick)

    def stop_welcome_pulse(self):
        """Stop the welcome subtitle pulse animation."""
        self._welcome_pulse_on = False
        after_id = getattr(self, "_welcome_pulse_after_id", None)
        if after_id:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self._welcome_pulse_after_id = None

    def show_capture_screen(self):
        self.clear_screens()
        self.capture_frame.pack(expand=True, fill="both")

    def show_filter_screen(self):
        self.clear_screens()
        self.filter_frame.pack(expand=True, fill="both")
        self._load_filter_background()

    def on_filter_selected(self, choice: str):
        self.filter_choice = choice
        print(f"[FILTER] Selected: {choice}")
        self.start_photo_sequence()

    def start_photo_sequence(self):
        """
        Start the photo capture sequence after filter selection.
        """
        self.show_capture_screen()

        self.photo_index = 1
        self.total_photos = getattr(self, "total_photos", 4)
        self.captured_files = []

        self.set_splash(True)

        # After short intro delay, begin first photo
        self.root.after(1200, self.start_next_photo)

    def start_sequence(self):
        """
        Entry point after successful payment.
        Must only run once per paid session.
        """
        self._code_screen_active = False
        self._stop_reader_waiting(cancel_reader=True, clear_payment_intent=False)

        if self.telemetry.is_out_of_paper():
            self.show_welcome_screen()
            return

        if not getattr(self, "session_paid", False):
            return

        if getattr(self, "is_running_sequence", False):
            return

        self.is_running_sequence = True

        try:
            telemetry.session_started()
        except Exception:
            pass

        try:
            try:
                self.stop_welcome_pulse()
            except Exception:
                pass

            self.show_filter_screen()

        except Exception as e:
            print("[ERROR] start_sequence failed:", e)
            self.is_running_sequence = False
            self.session_paid = False
            self.show_welcome_screen()

    def start_next_photo(self):
        if self.photo_index > self.total_photos:
            self.finish_sequence()
            return

        self.set_splash(False)

        # Show current photo number and prepare countdown
        self.capture_canvas.itemconfig(
            self.photo_item,
            text=f"Photo {self.photo_index}",
            state="normal"
        )
        self.capture_canvas.itemconfig(
            self.countdown_item,
            text="",
            state="normal"
        )

        self.run_countdown(3)

    def run_countdown(self, n: int):
        if n <= 0:
            self.capture_canvas.itemconfig(self.countdown_item, text="", state="hidden")
            self.capture_photo()
            return

        self.capture_canvas.itemconfig(self.countdown_item, text=str(n), state="normal")
        self.root.after(1000, lambda: self.run_countdown(n - 1))

    def finish_sequence(self):
        self.is_running_sequence = False
        self.photo_index = 1

        # Hide capture overlays
        self.capture_canvas.itemconfig(self.photo_item, state="hidden")
        self.capture_canvas.itemconfig(self.countdown_item, state="hidden")
        self.capture_canvas.itemconfig(self.splash_item, state="hidden")

        # Small pause before template picker
        self.root.after(400, self.show_template_picker_screen)

    def set_splash(self, visible: bool):
        self.capture_canvas.itemconfig(
            self.splash_item,
            state=("normal" if visible else "hidden")
        )
        if visible:
            self.capture_canvas.itemconfig(self.photo_item, state="hidden")
            self.capture_canvas.itemconfig(self.countdown_item, state="hidden")

    # -------------------------
    # Capture with gphoto2
    # -------------------------
    def capture_photo(self):
        def _do_capture():
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                output_path = INCOMING_DIR / f"photo_{self.photo_index}_{ts}.jpg"

                # Let the camera settle a little before triggering
                time.sleep(0.6)

                result_holder = {"saved_path": None, "error": None}

                def _run_camera():
                    try:
                        result_holder["saved_path"] = capture_photo_gphoto(output_path)
                    except Exception as e:
                        result_holder["error"] = e

                import threading
                camera_thread = threading.Thread(target=_run_camera, daemon=True)
                camera_thread.start()

                # Delay fake flash slightly so it lines up better with the real shutter
                time.sleep(0.18)

                self.root.after(0, lambda: self.capture_canvas.configure(bg="white"))
                self.root.after(0, self.root.update)

                time.sleep(0.10)

                self.root.after(0, lambda: self.capture_canvas.configure(bg="black"))
                self.root.after(0, self.root.update)

                camera_thread.join()

                if result_holder["error"] is not None:
                    raise result_holder["error"]

                saved_path = result_holder["saved_path"]
                self.captured_files.append(str(saved_path))
                print(f"[OK] Captured {saved_path}")

                def _advance_after_capture():
                    self.photo_index += 1
                    self.root.after(500, self.start_next_photo)

                self.root.after(0, _advance_after_capture)

            except Exception as e:
                print(f"[CAPTURE ERROR] Photo {self.photo_index} failed: {e}")

                def _handle_error():
                    messagebox.showerror(
                        "Camera Error",
                        f"Could not capture photo {self.photo_index}.\n\n{e}"
                    )
                    self.is_running_sequence = False
                    self.session_paid = False
                    self.show_welcome_screen()

                self.root.after(0, _handle_error)

        import threading
        threading.Thread(target=_do_capture, daemon=True).start()

    # -------------------------
    # Cleanup
    # -------------------------
    def build_template_picker_screen(self):
        self.template_frame = tk.Frame(self.container, bg="black")

        # Email message label at top (fixed, non-scrolling)
        self.template_email_label = tk.Label(
            self.template_frame,
            text="Have template ideas? Email them to skimuyu@stanford.edu",
            bg="black",
            fg="white",
            font=self.font_small
        )
        self.template_email_label.pack(pady=10)

        # Container for canvas + scrollbar
        self.template_container = tk.Frame(self.template_frame, bg="black")
        self.template_container.pack(expand=True, fill="both")

        self.template_canvas = tk.Canvas(
            self.template_container,
            bg="#eeeeee",
            highlightthickness=0,
            borderwidth=0
        )
        self.template_canvas.pack(side="left", expand=True, fill="both")

        self.template_scrollbar = tk.Scrollbar(
            self.template_container,
            orient="vertical",
            command=self.template_canvas.yview
        )
        self.template_scrollbar.pack(side="right", fill="y")

        self.template_canvas.configure(yscrollcommand=self.template_scrollbar.set)

        # Title
        self.template_title_item = self.template_canvas.create_text(
            self.root.winfo_screenwidth() // 2,
            80,
            text="Choose Your Template",
            fill="black",
            font=self.font_subtitle,
            anchor="center"
        )

        # Status / message text
        self.template_status_item = self.template_canvas.create_text(
            self.root.winfo_screenwidth() // 2,
            self.root.winfo_screenheight() // 2,
            text="",
            fill="black",
            font=self.font_title,
            anchor="center",
            state="hidden"
        )

        # Preview of composed strip
        self.template_preview_item = self.template_canvas.create_image(
            self.root.winfo_screenwidth() // 2,
            int(self.root.winfo_screenheight() * 0.55),
            anchor="center",
            image=None,
            state="hidden"
        )

        # Keep refs so Tk does not garbage collect images
        self._template_thumb_refs = []
        self._template_item_to_path = {}
        self._template_grid_item_ids = []

    def _enable_touch_scroll(self):
        self.template_canvas.bind("<ButtonPress-1>", self._start_touch_scroll)
        self.template_canvas.bind("<B1-Motion>", self._do_touch_scroll)

    def _start_touch_scroll(self, event):
        self.template_canvas.scan_mark(event.x, event.y)

    def _do_touch_scroll(self, event):
        self.template_canvas.scan_dragto(event.x, event.y, gain=1)

    def _prepare_template_overlay_message(self) -> tuple[int, int]:
        """Disable finger scroll and scrollbar; scrollregion exactly matches canvas (no extra scroll)."""
        self.template_canvas.unbind("<ButtonPress-1>")
        self.template_canvas.unbind("<B1-Motion>")
        self.template_scrollbar.pack_forget()
        self.template_frame.update_idletasks()
        self.root.update_idletasks()
        self.root.update()
        cw = self.template_canvas.winfo_width()
        ch = self.template_canvas.winfo_height()
        if cw < 20 or ch < 20:
            self.template_frame.update()
            self.root.update()
            cw = self.template_canvas.winfo_width()
            ch = self.template_canvas.winfo_height()
        # Must not use screen size here — a scrollregion larger than the widget enables scrolling.
        cw = max(cw, 1)
        ch = max(ch, 1)
        self.template_canvas.configure(scrollregion=(0, 0, cw, ch))
        self.template_canvas.yview_moveto(0)
        self.template_canvas.xview_moveto(0)
        self._template_overlay_cw = cw
        self._template_overlay_ch = ch
        return cw, ch

    def _place_template_status_centered(self, cw: int, ch: int):
        self.template_canvas.coords(self.template_status_item, cw // 2, ch // 2)
        self.template_canvas.itemconfig(
            self.template_status_item,
            width=max(320, int(cw * 0.85)),
        )

    def _restore_template_scroll_chrome(self):
        """Show scrollbar again after a fullscreen overlay (printing / done)."""
        try:
            self.template_scrollbar.pack_info()
        except tk.TclError:
            self.template_scrollbar.pack(side="right", fill="y")

    def show_template_picker_screen(self):
        self.clear_screens()
        self.template_frame.pack(expand=True, fill="both")

        self._restore_template_scroll_chrome()

        # Reset status and preview
        self.template_canvas.itemconfig(
            self.template_status_item,
            state="hidden",
            text="",
            width=0,
        )
        self.template_canvas.itemconfig(
            self.template_preview_item,
            image=None,
            state="hidden"
        )

        self._composed_preview_ref = None
        self.last_composed_strip = None

        self.render_template_grid()
        self._enable_touch_scroll()

    def render_template_grid(self):
        # Clear old thumbnail canvas items
        for item_id in self._template_grid_item_ids:
            try:
                self.template_canvas.delete(item_id)
            except Exception:
                pass

        self._template_grid_item_ids.clear()
        self._template_thumb_refs.clear()

        # Hide/clear prior status
        self.template_canvas.itemconfig(
            self.template_status_item,
            state="hidden",
            text="",
            width=0,
        )

        # Load templates (PNG + JSON pairs)
        try:
            self.templates = load_templates(self.templates_dir)
            print("self.templates type:", type(self.templates))
            print(
                "TEMPLATES LOADED IN GRID:",
                len(self.templates) if self.templates is not None else "None",
                "from",
                self.templates_dir
            )
        except Exception as e:
            print("\n--- TEMPLATE GRID ERROR TRACEBACK ---")
            traceback.print_exc()
            print("--- END TRACEBACK ---\n")

            self.template_canvas.itemconfig(
                self.template_status_item,
                state="normal",
                text=f"Template load error:\n{e}",
                width=900
            )
            self.template_canvas.tag_raise(self.template_status_item)
            self.templates = []
            self.template_canvas.configure(scrollregion=(0, 0, 0, 0))
            return

        if not self.templates:
            self.template_canvas.itemconfig(
                self.template_status_item,
                state="normal",
                text=f"No templates found in:\n{self.templates_dir}",
                width=900
            )
            self.template_canvas.tag_raise(self.template_status_item)
            self.template_canvas.configure(scrollregion=(0, 0, 0, 0))
            return

        # Hide the fixed page title; section headers below act as the headings
        # so "First Day of Classes Specials" is the first thing on the page.
        self.template_canvas.itemconfig(self.template_title_item, state="hidden")

        # ---- Curated layout: First Day of Classes Specials, then Classics ----
        # Specials are placed by expected filename stem (TemplateDef.key). Files
        # for the 14th-17th can be dropped in later with these exact names and
        # they will fill the empty slots automatically. Naming pattern matches
        # the existing 13th templates.
        SPECIAL_DATES = (13, 14, 15, 16, 17)
        NOTEBOOK_STEMS = [f"FDof{d}thNotebookTemplate" for d in SPECIAL_DATES]
        SPARKLES_STEMS = [f"FDO{d}thGwSparklesTemplate" for d in SPECIAL_DATES]
        OVERLAY_STEMS = ["FDO13thGwOverlaysTemplate"]

        by_key = {t.key: t for t in self.templates}
        special_keys = set(NOTEBOOK_STEMS) | set(SPARKLES_STEMS) | set(OVERLAY_STEMS)

        # Classics = every template that is not one of the FDo... specials,
        # in the order load_templates returned them.
        classic_defs = [t for t in self.templates if t.key not in special_keys]

        # ---- layout metrics ----
        screen_w = self.root.winfo_screenwidth()
        pad = 18
        side_margin = 24
        bottom_scroll_padding = 260
        cols = 5  # specials rows are 5-wide; classics reuse the same grid

        thumb_w = int((screen_w - side_margin * 2 - pad * (cols - 1)) / cols)
        thumb_h = int(thumb_w * 1.2)

        def col_center_x(col: int) -> int:
            return side_margin + col * (thumb_w + pad) + thumb_w // 2

        def draw_section_header(text: str, top_y: int) -> int:
            item = self.template_canvas.create_text(
                side_margin,
                top_y,
                text=text,
                fill="black",
                font=self.font_subtitle,
                anchor="w",
            )
            self._template_grid_item_ids.append(item)
            return top_y + 70  # space below header before the first row

        def draw_tile(tdef: TemplateDef, cx: int, cy: int):
            try:
                img = Image.open(tdef.png_path).convert("RGBA")
                img.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
                tk_img = ImageTk.PhotoImage(img)
            except Exception as e:
                print(f"[WARN] Could not load template {tdef.png_path}: {e}")
                return
            self._template_thumb_refs.append(tk_img)
            item_id = self.template_canvas.create_image(
                cx, cy, image=tk_img, anchor="center"
            )
            self._template_grid_item_ids.append(item_id)
            self.template_canvas.tag_bind(
                item_id,
                "<Button-1>",
                lambda e, td=tdef: self.on_template_selected(td),
            )

        def draw_placeholder(label: str, cx: int, cy: int):
            """Dashed box for a special whose PNG has not been added yet."""
            left = cx - thumb_w // 2
            top = cy - thumb_h // 2
            rect = self.template_canvas.create_rectangle(
                left, top, left + thumb_w, top + thumb_h,
                outline="#bbbbbb", dash=(6, 4), width=2,
            )
            txt = self.template_canvas.create_text(
                cx, cy, text=label, fill="#999999",
                font=("Helvetica", 18, "normal"),
                anchor="center", justify="center", width=thumb_w - 16,
            )
            self._template_grid_item_ids.extend([rect, txt])

        def draw_specials_row(stems, top_y: int) -> int:
            """One 5-wide row placed by filename stem; gaps get placeholders."""
            for col, (stem, date) in enumerate(zip(stems, SPECIAL_DATES)):
                cx = col_center_x(col)
                cy = top_y + thumb_h // 2
                tdef = by_key.get(stem)
                if tdef is not None:
                    draw_tile(tdef, cx, cy)
                else:
                    draw_placeholder(f"{date}th\n(coming soon)", cx, cy)
            return top_y + thumb_h + pad

        y = 120  # top of scrollable content

        # ---- Section 1: First Day of Classes Specials ----
        y = draw_section_header("First Day of Classes Specials", y)
        y = draw_specials_row(NOTEBOOK_STEMS, y)
        y = draw_specials_row(SPARKLES_STEMS, y)
        # Row 3: the single "1 with overlays" (left-aligned, or placeholder)
        overlay_tdef = by_key.get(OVERLAY_STEMS[0])
        cy = y + thumb_h // 2
        if overlay_tdef is not None:
            draw_tile(overlay_tdef, col_center_x(0), cy)
        else:
            draw_placeholder("overlays\n(coming soon)", col_center_x(0), cy)
        y = y + thumb_h + pad

        # ---- Section 2: Classic Photostrips ----
        y += 24  # extra breathing room before the next heading
        y = draw_section_header("Classic Photostrips:", y)
        for idx, tdef in enumerate(classic_defs):
            col = idx % cols
            row = idx // cols
            cx = col_center_x(col)
            cy = y + row * (thumb_h + pad) + thumb_h // 2
            draw_tile(tdef, cx, cy)

        self.template_canvas.update_idletasks()
        bbox = self.template_canvas.bbox("all")
        if bbox:
            self.template_canvas.configure(
                scrollregion=(bbox[0], bbox[1], bbox[2], bbox[3] + bottom_scroll_padding)
            )
        else:
            self.template_canvas.configure(scrollregion=(0, 0, 0, 0))

    def on_template_selected(self, tdef: TemplateDef):
        needed = getattr(self, "total_photos", 4)

        # Guard: ensure we have all captured photos
        if len(self.captured_files) < needed:
            self.template_canvas.itemconfig(
                self.template_status_item,
                state="normal",
                text=f"Missing photos ({len(self.captured_files)}/{needed})."
            )
            self.template_canvas.tag_raise(self.template_status_item)
            return

        # Telemetry + analytics: remember template label for this session
        template_id = (
            getattr(tdef, "template_id", None)
            or getattr(tdef, "name", None)
            or Path(tdef.png_path).stem
        )
        self.selected_template_name = str(template_id)
        try:
            self.telemetry.template_chosen(self.selected_template_name)
        except Exception:
            pass

        # Hide template grid and title; printing message appears after compose succeeds
        self.hide_template_grid()
        self.template_canvas.itemconfig(self.template_title_item, state="hidden")
        self.template_canvas.itemconfig(
            self.template_status_item,
            state="hidden",
            text="",
        )

        try:
            print("[DEBUG] Captured files going into compose:")
            for i, p in enumerate(self.captured_files[:needed], start=1):
                print(f"  [{i}] {p}")

            composed_img = compose(
                tdef.png_path,
                tdef.slots,
                self.captured_files[:needed],
                self.filter_choice,
                tdef.layer_mode,
            )

            double_img = side_by_side(composed_img)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_name = f"strip_double_{ts}.jpg"
            out_path = Path(__file__).resolve().parent / out_name

            rgb_img = double_img.convert("RGB")
            rgb_img.save(out_path, format="JPEG", quality=95)
            self.last_composed_strip = str(out_path)

            print("[OK] Saved doubled strip:", out_path)

            try:
                delete_files([Path(p) for p in self.captured_files[:needed]])
                self.captured_files = []
                print("[CLEANUP] Deleted original captured camera photos.")
            except Exception as cleanup_error:
                print("[WARN] Could not delete original captured files:", cleanup_error)

        except Exception as e:
            self.template_canvas.itemconfig(
                self.template_status_item,
                state="normal",
                text=f"Compose error:\n{e}"
            )
            self.template_canvas.tag_raise(self.template_status_item)
            print("[COMPOSE ERROR]", e)
            return

        # Show printing message immediately
        self.show_printing_message()

        # Let Tkinter actually draw the message, then start printing in background
        import threading
        self.root.after(
            100,
            lambda: threading.Thread(
                target=lambda: self._print_then_done(str(out_path)),
                daemon=True
            ).start()
        )

    def show_printing_message(self):
        cw, ch = self._prepare_template_overlay_message()
        self._place_template_status_centered(cw, ch)
        self.template_canvas.itemconfig(
            self.template_status_item,
            state="normal",
            text="Printing your photos...\nPlease wait!",
            fill="black",
        )
        self.template_canvas.tag_raise(self.template_status_item)
        self.root.update_idletasks()

    def print_file(self, file_path: str) -> tuple[bool, str]:
        """
        Print a composed image on Windows (GDI / default or configured printer).
        Returns (ok, short details) for telemetry.
        """
        print(f"[PRINT] Starting print attempt for: {file_path}")
        print(f"[PRINT] Printer name: {self.printer_name or 'default'}")

        try:
            # Check if pywin32 is available
            import win32print
            import win32ui
            from PIL import Image, ImageWin
            print("[PRINT] pywin32 modules imported successfully")
        except ImportError as e:
            error_msg = f"pywin32 not installed. Run: pip install pywin32"
            print(f"[PRINT ERROR] {error_msg}")
            return False, error_msg

        try:
            print_image_windows(file_path, self.printer_name)
            print("[PRINT] Print job completed successfully")
            return True, f"sent to printer: {self.printer_name or 'default'}"
        except Exception as e:
            details = str(e).strip()
            print(f"[PRINT ERROR] Print failed: {details}")
            return False, details[:400]

    def _print_then_done(self, composed_path: str):
        """
        Print the composed strip, then clean up all temporary files and
        return to the welcome screen.
        """
        ok, details = self.print_file(composed_path)

        # Telemetry: print result
        try:
            self.telemetry.print_result(ok=ok, details=details)
        except Exception as e:
            print(f"Failed to log print result: {e}")

        if not ok:
            self.root.after(
                0,
                lambda: self._show_print_failed()
            )
            return

        try:
            log_print(getattr(self, "selected_template_name", None) or "unknown")
        except Exception as e:
            print(f"Failed to log print to analytics DB: {e}")

        # Telemetry: successful session + template used
        try:
            self.telemetry.session_completed(
                template_id=getattr(self, "selected_template_name", "unknown")
            )
            print("Logged session_completed")
        except Exception as e:
            print(f"Failed to log session_completed: {e}")

        PRINT_TIME_MS = 30000  # adjust if needed

        self.root.after(
            PRINT_TIME_MS,
            self._show_print_done
        )

    def _show_print_failed(self):
        cw, ch = self._prepare_template_overlay_message()
        self._place_template_status_centered(cw, ch)
        self.template_canvas.itemconfig(
            self.template_status_item,
            state="normal",
            text="Print failed.\nReturning to start.",
            fill="black",
        )
        self.template_canvas.tag_raise(self.template_status_item)
        self.root.after(4000, self.cleanup_session_files_and_return)

    def _show_print_done(self):
        cw, ch = self._prepare_template_overlay_message()
        self._place_template_status_centered(cw, ch)
        self.template_canvas.itemconfig(
            self.template_status_item,
            state="normal",
            text="Done Printing! Collect Your Photos Outside!",
            fill="black",
        )
        self.template_canvas.tag_raise(self.template_status_item)
        self.root.after(12000, self.cleanup_session_files_and_return)

    def shutdown_booth(self):
        if getattr(self, "_is_shutting_down", False):
            return
        self._is_shutting_down = True
        self._stop_reader_waiting(cancel_reader=True, clear_payment_intent=True)

        print("Shutting down booth...")

        try:
            self.telemetry.log_event("booth_stopped")
            print("Logged booth_stopped")
        except Exception as e:
            print(f"Failed to log booth_stopped: {e}")

        try:
            self.telemetry.send_telegram(f"🛑 Booth stopped ({self.telemetry.booth_id})")
            print("Telegram sent")
        except Exception as e:
            print(f"Failed to send Telegram stop message: {e}")

        try:
            for f in list(getattr(self, "captured_files", []) or []):
                try:
                    Path(f).unlink(missing_ok=True)
                except Exception as e:
                    print(f"Failed to delete file {f}: {e}")

            last_strip = getattr(self, "last_composed_strip", None)
            if last_strip:
                try:
                    Path(last_strip).unlink(missing_ok=True)
                except Exception as e:
                    print(f"Failed to delete strip {last_strip}: {e}")
        finally:
            try:
                self.root.destroy()
            except Exception as e:
                print(f"Failed to destroy root: {e}")

    def cleanup_and_exit(self):
        self.shutdown_booth()
    def cleanup_session_files_and_return(self):
        """
        Delete all temporary session files:
        - original captured camera photos
        - composed strip file
        Then reset booth state and return to welcome screen.
        """
        deleted = 0

        # Delete captured original photos from computer
        for f in list(getattr(self, "captured_files", []) or []):
            try:
                p = Path(f)
                print("[CLEANUP] trying delete captured photo:", p)

                if p.exists():
                    p.unlink()
                    deleted += 1
                    print("[CLEANUP] deleted captured photo:", p)
                else:
                    print("[CLEANUP] captured photo not found:", p)

            except Exception as e:
                print(f"[WARN] Could not delete captured photo {f}: {e}")

        # Delete composed strip from computer
        last_strip = getattr(self, "last_composed_strip", None)
        if last_strip:
            try:
                p = Path(last_strip)
                print("[CLEANUP] trying delete composed strip:", p)

                if p.exists():
                    p.unlink()
                    deleted += 1
                    print("[CLEANUP] deleted composed strip:", p)
                else:
                    print("[CLEANUP] composed strip not found:", p)

            except Exception as e:
                print(f"[WARN] Could not delete composed strip {last_strip}: {e}")

        # Reset session state
        self.captured_files = []
        self.last_composed_strip = None
        self._payment_in_progress = False
        self.session_paid = False
        self.is_running_sequence = False
        self.photo_index = 1
        self._clear_reader_payment_intent()

        print(f"[CLEANUP] done. deleted {deleted} files.")

        # Return to welcome screen
        self.show_welcome_screen()

    def hide_template_grid(self):
        print("HIDING TEMPLATE COUNT:", len(self._template_grid_item_ids))
        for item_id in self._template_grid_item_ids:
            try:
                self.template_canvas.itemconfig(item_id, state="hidden")
            except Exception:
                pass

def main():
    run_dashboard()
    root = tk.Tk()
    root.attributes("-fullscreen", True)

    # Heartbeat every 1 hour
    def schedule_heartbeat():
        try:
            telemetry.heartbeat(uptime_start=START_TIME)
        except Exception as e:
            print(f"Heartbeat failed: {e}")
        root.after(60 * 60 * 1000, schedule_heartbeat)

    schedule_heartbeat()

    # Build UI ONCE
    app = PhotoBoothUI(root, telemetry)

    def handle_sigint(sig, frame):
        app.shutdown_booth()
        sys.exit(0)

    root.protocol("WM_DELETE_WINDOW", app.shutdown_booth)
    signal.signal(signal.SIGINT, handle_sigint)

    root.mainloop()

if __name__ == "__main__":
    main()
