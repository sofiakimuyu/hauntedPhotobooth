

# Import all the libraries you need
from datetime import datetime
import tkinter as tk
from tkinter import messagebox
import json
import stripe
from PIL import Image, ImageTk
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

# Define all the important functions we will need later

#Wait for a newly imported JPG to appear in INCOMING_DIR.  Returns the Path to the new file, or None if timed out.
def to_wsl_path(path: Path) -> str:
    """
    Convert a Windows path like:
        C:\photobooth\incoming\img.jpg
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
            slots_raw = data.get("slots") or []

            if len(slots_raw) != 4:
                raise ValueError("Template must define exactly 4 slots")

            defaults = data.get("defaults", {})
            slots: list[Slot] = []

            for s in slots_raw:
                merged = {**defaults, **s}
                slots.append(
                    Slot(
                        id=int(merged["id"]),
                        x=int(merged["x"]),
                        y=int(merged["y"]),
                        w=int(merged["w"]),
                        h=int(merged["h"]),
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


def compose(template_png_path: str, slots: List[Slot], photo_paths: List[str]) -> Image.Image:
    """
    Fill a template background with the 4 captured photos in slot order.
    Returns one final composed PIL image.
    """
    template_path = Path(template_png_path)

    with Image.open(template_path) as im:
        base = im.convert("RGBA")

    if len(photo_paths) != 4:
        raise ValueError(f"Expected exactly 4 photo paths, got {len(photo_paths)}.")

    slots_sorted = sorted(slots, key=lambda s: s.id)
    if len(slots_sorted) != 4:
        raise ValueError(
            f"Template must define exactly 4 slots, got {len(slots_sorted)}."
        )

    for slot, photo_path in zip(slots_sorted, photo_paths):
        photo_path = Path(photo_path)

        if not photo_path.is_absolute():
            photo_path = BASE_DIR / photo_path

        with Image.open(photo_path) as im:
            img = im.convert("RGBA")

        if slot.rotation:
            img = img.rotate(slot.rotation, expand=True)

        fitted = cover_resize(img, slot.w, slot.h).convert("RGBA")
        mask = fitted.getchannel("A")
        base.paste(fitted, (slot.x, slot.y), mask)

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

        print(
            "WELCOME BG PATH:",
            self.welcome_bg_path,
            "exists?",
            os.path.exists(self.welcome_bg_path)
        )

        self.tk_welcome_bg = None

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
        self.build_capture_screen()
        self.build_template_picker_screen()

        # Staff: after loading a new paper roll (telemetry roll limit was hit)
        self.root.bind("<Control-Shift-P>", self._operator_clear_paper_lock)
        self.root.bind("<Control-Shift-p>", self._operator_clear_paper_lock)

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

    def charge_with_wisepos(self, amount_cents: int) -> bool:
        """
        Run a Stripe Terminal (WisePOS) payment and wait for completion.
        Returns True if payment succeeds.
        """
        try:
            # 1. Create PaymentIntent
            pi = stripe.PaymentIntent.create(
                amount=amount_cents,
                currency="usd",
                payment_method_types=["card_present"],
                description="Photobooth session"
            )

            # 2. Send to reader
            stripe.terminal.Reader.process_payment_intent(
                STRIPE_READER_ID,
                payment_intent=pi.id
            )

            # 3. Wait for reader action to complete
            start = time.time()
            while time.time() - start < 90:
                reader = stripe.terminal.Reader.retrieve(STRIPE_READER_ID)
                action = getattr(reader, "action", None)

                if not action:
                    time.sleep(0.5)
                    continue

                status = getattr(action, "status", None)

                if status == "succeeded":
                    pi = stripe.PaymentIntent.retrieve(pi.id)
                    return pi.status == "succeeded"

                if status in ("failed", "canceled"):
                    return False

                time.sleep(0.5)

            # 4. Timeout → cancel
            try:
                stripe.terminal.Reader.cancel_action(STRIPE_READER_ID)
            except Exception:
                pass

            return False

        except Exception as e:
            try:
                stripe.terminal.Reader.cancel_action(STRIPE_READER_ID)
            except Exception:
                pass

            messagebox.showerror("Payment error", str(e))
            return False

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
            text="Tap to pay & start",
            fill="white",
            font=self.font_subtitle,
            anchor="center"
        )

        # Welcome/payment state
        self.session_paid = False
        self._payment_in_progress = False
        self._welcome_tap_enabled = True

        def _on_welcome_tap(event=None):
            if self.telemetry.is_out_of_paper():
                return
            if (
                    not self._welcome_tap_enabled
                    or self.session_paid
                    or self._payment_in_progress
            ):
                return

            self._payment_in_progress = True
            self.welcome_canvas.itemconfig(
                self.subtitle_item,
                text="Please pay on the terminal…"
            )

            def _run_payment():
                ok = False
                try:
                    # Clear any stuck reader action first
                    try:
                        stripe.terminal.Reader.cancel_action(STRIPE_READER_ID)
                    except Exception:
                        pass

                    ok = self.charge_with_wisepos(amount_cents=50)

                finally:
                    def _finish():
                        self._payment_in_progress = False

                        if ok:
                            self.session_paid = True
                            self._welcome_tap_enabled = False
                            self.welcome_canvas.itemconfig(
                                self.subtitle_item,
                                text="Paid ✅ Starting…"
                            )
                            self.root.after(100, self.start_sequence)
                        else:
                            self.welcome_canvas.itemconfig(
                                self.subtitle_item,
                                text="Payment failed. Tap to try again."
                            )

                    self.root.after(0, _finish)

            import threading
            threading.Thread(target=_run_payment, daemon=True).start()

        self.welcome_canvas.bind("<Button-1>", _on_welcome_tap)

        def _tap_wrapper(event=None):
            if not self._welcome_tap_enabled:
                return
            _on_welcome_tap(event)

        # Bind tap/click across the whole welcome screen
        self.welcome_canvas.bind("<Button-1>", _tap_wrapper)
        self.welcome_canvas.tag_bind(self.welcome_bg_item, "<Button-1>", _tap_wrapper)
        self.welcome_canvas.tag_bind(self.welcome_text_item, "<Button-1>", _tap_wrapper)
        self.welcome_canvas.tag_bind(self.subtitle_item, "<Button-1>", _tap_wrapper)

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

    # -------------------------
    # Screen navigation
    # -------------------------
    def clear_screens(self):
        self.stop_welcome_pulse()
        for child in self.container.winfo_children():
            child.pack_forget()

    def show_welcome_screen(self):
        self.clear_screens()
        self.welcome_frame.pack(expand=True, fill="both")

        # Reset session/run state
        self.is_running_sequence = False
        self.session_paid = False
        self._payment_in_progress = False

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
                text="Tap to pay & start",
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

    def start_sequence(self):
        """
        Entry point after successful payment.
        Must only run once per paid session.
        """
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

            self.show_capture_screen()

            self.photo_index = 1
            self.total_photos = getattr(self, "total_photos", 4)
            self.captured_files = []

            self.set_splash(True)

            # After short intro delay, begin first photo
            self.root.after(1200, self.start_next_photo)

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

        # Grid layout
        screen_w = self.root.winfo_screenwidth()
        start_y = 140
        pad = 30
        cols = 3

        thumb_w = int((screen_w - pad * (cols + 1)) / cols)
        thumb_h = int(thumb_w * 1.2)

        # Create thumbnails
        for idx, tdef in enumerate(self.templates):
            col = idx % cols
            row = idx // cols

            x = pad + col * (thumb_w + pad) + thumb_w // 2
            y = start_y + row * (thumb_h + pad) + thumb_h // 2

            try:
                img = Image.open(tdef.png_path).convert("RGBA")
                img.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
                tk_img = ImageTk.PhotoImage(img)
            except Exception as e:
                print(f"[WARN] Could not load template {tdef.png_path}: {e}")
                continue

            self._template_thumb_refs.append(tk_img)

            item_id = self.template_canvas.create_image(
                x,
                y,
                image=tk_img,
                anchor="center"
            )
            self._template_grid_item_ids.append(item_id)

            self.template_canvas.tag_bind(
                item_id,
                "<Button-1>",
                lambda e, td=tdef: self.on_template_selected(td)
            )

        self.template_canvas.update_idletasks()
        bbox = self.template_canvas.bbox("all")
        if bbox:
            self.template_canvas.configure(scrollregion=bbox)
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
                self.captured_files[:needed]
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