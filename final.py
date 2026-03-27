# with canvas

"""
Canvas-first PhotoBooth UI (Tkinter)

Features:
- Welcome screen (Canvas) with optional background image
- "Pay (demo)" button unlocks Start
- Start -> "3 photos, get ready!" (2s) -> countdown 3,2,1 (1s each)
- Runs countdown + capture for Photo 1, Photo 2, Photo 3
- Top-right shows "Photo X" during countdown
- Background on capture screen is live camera preview if OpenCV+Pillow available; otherwise solid black
- Captures save to JPG if preview frames exist; otherwise simulates capture

Controls:
- ESC exits

Dependencies (optional for camera preview + saving frames):
    pip3 install opencv-python pillow
"""

from datetime import datetime
import tkinter as tk
from tkinter import messagebox
import json
import stripe
from PIL import Image, ImageTk
from pathlib import Path
import os
import traceback
import subprocess
import platform
from telemetry import Telemetry
import glob
import subprocess
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
import time



telemetry = Telemetry(
    booth_id="Stanford booth",
    telegram_token="8537687055:AAHtcNOv2lQYRmMBvcDNK8idGF2EmUnT7c",
    telegram_chat_id="8471596713",
    roll_warn_1=143,
    roll_warn_2=147
)

telemetry.install_crash_hook()
START_TIME = time.time()
telemetry.booth_started()
# to keep track in case I have other booths in future

# 🔴 TEMP TEST — REMOVE AFTER VERIFYING
telemetry.print_result(ok=False, details="test fail")
telemetry.print_result(ok=True, details="test success")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "PhotoboothTemplates"

stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
STRIPE_READER_ID = os.environ["STRIPE_READER_ID"]


# Optional preview stack
try:
    import cv2
    from PIL import Image, ImageTk
    HAS_PREVIEW = True
except Exception:
    HAS_PREVIEW = False

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

def load_templates(dir_path) -> list[TemplateDef]:
    # Accept str OR Path safely
    dir_path = Path(dir_path)

    if not dir_path.is_dir():
        raise FileNotFoundError(f"Missing folder: {dir_path}")

    # Collect png/json pairs by stem
    pngs = {p.stem: p for p in dir_path.glob("*.png")}
    jsons = {p.stem: p for p in dir_path.glob("*.json")}

    keys = sorted(set(pngs.keys()) & set(jsons.keys()))
    if not keys:
        raise FileNotFoundError(
            f"No templates found in {dir_path}\n"
            f"Expected matching .png + .json pairs"
        )

    templates: list[TemplateDef] = []

    for key in keys:
        try:
            print("Loading template key:", key)

            png_path = pngs[key]
            json_path = jsons[key]

            with json_path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            name = data.get("name", key)

            # Safely handle null / missing slots
            slots_raw = data.get("slots") or []
            if len(slots_raw) != 4:
                raise ValueError("must define exactly 4 slots")

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

    print("ABOUT TO RETURN templates length:", len(templates))
    return templates



def cover_resize(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Resize to cover target box, then center crop (no stretching)."""
    iw, ih = img.size
    scale = max(target_w / iw, target_h / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    img = img.resize((nw, nh), Image.LANCZOS)

    left = (nw - target_w) // 2
    top = (nh - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))



def compose(template_png_path: str, slots: List[Slot], photo_paths: List[str]) -> Image.Image:
    """Compose the template with photos in slot-id order (robust alpha-safe paste)."""
    base = Image.open(template_png_path).convert("RGBA")

    # Expect exactly 4 photos for now
    if len(photo_paths) != 4:
        raise ValueError(f"Expected exactly 4 photo paths (got {len(photo_paths)}).")

    # Ensure slot order is 1..4
    slots_sorted = sorted(slots, key=lambda s: s.id)
    if len(slots_sorted) != 4:
        raise ValueError(f"Template JSON must define exactly 4 slots (got {len(slots_sorted)}).")

    base_dir = os.path.dirname(__file__)

    for slot, photo_path in zip(slots_sorted, photo_paths):
        photo_path = str(photo_path)
        if not os.path.isabs(photo_path):
            photo_path = os.path.join(base_dir, photo_path)

        # Open + normalize to RGBA so we always have an alpha channel to use as a mask
        with Image.open(photo_path) as im:
            img = im.convert("RGBA")

        if getattr(slot, "rotation", 0):
            img = img.rotate(slot.rotation, expand=True)

        # cover_resize might return RGB depending on its implementation, so enforce RGBA after
        fitted = cover_resize(img, slot.w, slot.h)
        if fitted.mode != "RGBA":
            fitted = fitted.convert("RGBA")

        # ✅ Valid mask: alpha channel (mode 'L') — prevents "bad transparency mask"
        mask = fitted.split()[-1]
        base.paste(fitted, (slot.x, slot.y), mask)

    return base


def side_by_side(
    img: Image.Image,
    gap: int = 40,
    bg: Tuple[int, int, int, int] = (255, 255, 255, 255)
) -> Image.Image:
    """
    Duplicate the composed template image and place two copies side-by-side.
    """
    img = img.convert("RGBA")
    w, h = img.size

    out = Image.new("RGBA", (w * 2 + gap, h), bg)
    out.paste(img, (0, 0), img)
    out.paste(img, (w + gap, 0), img)

    return out

def print_image_windows(image_path: str, printer_name: str | None = None):
    """
    Print an image on Windows using MS Paint.
    If printer_name is None, Windows default printer is used.
    """
    if platform.system() != "Windows":
        print("[PRINT] Skipped (not Windows)")
        return

    mspaint = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "mspaint.exe"
    )

    if printer_name:
        cmd = [mspaint, "/pt", image_path, printer_name]
    else:
        cmd = [mspaint, "/pt", image_path]

    print("[PRINT] Sending to printer:", image_path)
    subprocess.run(cmd, check=True)



class PhotoBoothUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PhotoBooth")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="black")
        self.root.bind("<Escape>", lambda e: self.cleanup_and_exit())
        self.templates = []
        # ---- state ----
        self.is_running_sequence = False
        self.photo_index = 1  # 1..3
        self.total_photos = 4

        # ---- template / printing state ----
        self.captured_files = []          # stores the 3 photo filenames from a session
        self.templates_dir = Path(__file__).resolve().parent / "PhotoboothTemplates"  # folder containing photo strip templates

        self.printer_name = None          # optional: set to printer queue name
        self.last_composed_strip = None   # final composed strip image path

        # ---- camera ----
        self.cap = None
        self.latest_frame_bgr = None
        self.tk_preview_img = None  # keep ref to prevent GC

        # Try to open default camera (Mac webcam as placeholder)
        if HAS_PREVIEW:
            self.cap = cv2.VideoCapture(0)
            if not self.cap.isOpened():
                self.cap = None

        # ---- assets (optional) ----
        # If you want a welcome background image, set this path to your file.
        # Example: "assets/welcome_bg.jpg"
        self.welcome_bg_path = os.path.join(os.path.dirname(__file__), "stanfordwelcomebackground.png")
        print("WELCOME BG PATH:", self.welcome_bg_path, "exists?", os.path.exists(self.welcome_bg_path))
        self.tk_welcome_bg = None  # keep ref if loaded

        # ---- fonts (use system fonts; you can change family names) ----
        self.font_title = ("Helvetica", 64, "bold")
        self.font_subtitle = ("Helvetica", 28, "normal")
        self.font_button = ("Helvetica", 28, "bold")
        self.font_small = ("Helvetica", 24, "bold")
        self.font_countdown = ("Helvetica", 160, "bold")

        # ---- container ----
        self.container = tk.Frame(self.root, bg="black")
        self.container.pack(expand=True, fill="both")

        # ---- build screens ----
        self.build_welcome_screen()
        self.build_capture_screen()
        self.build_template_picker_screen()

        # start on welcome
        self.show_welcome_screen()

        # start preview loop
        self.update_camera_frame()

    def charge_with_wisepos(self, amount_cents: int) -> bool:
        """
        Starts a card-present payment on the WisePOS and waits for completion.
        Returns True only when the payment succeeds.
        """
        try:
            # 1) Create the PaymentIntent (it will usually start as requires_payment_method — that's normal)
            pi = stripe.PaymentIntent.create(
                amount=amount_cents,
                currency="usd",
                payment_method_types=["card_present"],
                description="Photobooth session"
            )

            # 2) Tell the reader to process it
            stripe.terminal.Reader.process_payment_intent(
                STRIPE_READER_ID,
                payment_intent=pi.id
            )

            # 3) Wait for the READER action to finish (this is the reliable signal)
            start = time.time()
            while time.time() - start < 90:
                reader = stripe.terminal.Reader.retrieve(STRIPE_READER_ID)
                action = getattr(reader, "action", None)

                # If no action yet, just keep waiting a moment
                if not action:
                    time.sleep(0.5)
                    continue

                status = getattr(action, "status", None)

                # Action finished successfully → confirm PI status
                if status == "succeeded":
                    pi = stripe.PaymentIntent.retrieve(pi.id)
                    return pi.status == "succeeded"

                # Action ended unsuccessfully
                if status in ("failed", "canceled"):
                    return False

                # Otherwise it's still in progress
                time.sleep(0.5)

            # 4) Timeout: cancel on reader to reset terminal state
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

        # Optional: load a welcome background image (fills screen)
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
            0, 0,
            anchor="nw",
            image=self.tk_welcome_bg
        )
        self.welcome_canvas.tag_lower(self.welcome_bg_item)

        # Title text
        self.welcome_text_item = self.welcome_canvas.create_text(
            self.root.winfo_screenwidth() // 2,
            200,
            text="Welcome",
            fill="white",
            font=self.font_title,
            anchor="center"
        )

        # Subtitle
        self.subtitle_item = self.welcome_canvas.create_text(
            self.root.winfo_screenwidth() // 2,
            300,
            text="Tap to pay & start",
            fill="white",
            font=self.font_subtitle,
            anchor="center"
        )

        # --- Welcome tap-to-pay state ---
        self.session_paid = False
        self._payment_in_progress = False
        self._welcome_tap_enabled = True

        def _on_welcome_tap(event=None):
            if self.session_paid or self._payment_in_progress:
                return

            self._payment_in_progress = True
            self.welcome_canvas.itemconfig(self.subtitle_item, text="Please pay on the terminal…")

            def _run_payment():
                ok = False
                try:
                    # Clear any stuck reader action (safe)
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
                            self.welcome_canvas.itemconfig(self.subtitle_item, text="Paid ✅ Starting…")
                            self.root.after(100, self.start_sequence)
                        else:
                            self.welcome_canvas.itemconfig(self.subtitle_item, text="Payment failed. Tap to try again.")

                    self.root.after(0, _finish)

            import threading
            threading.Thread(target=_run_payment, daemon=True).start()

        def _tap_wrapper(event=None):
            if not self._welcome_tap_enabled:
                return
            _on_welcome_tap(event)

        # Bind tap/click on the entire welcome screen
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

        # Background preview image (updated each frame)
        self.bg_image_item = self.capture_canvas.create_image(
            0, 0,
            anchor="nw",
            image=None
        )
        self.capture_canvas.tag_lower(self.bg_image_item)

        # Splash: "3 photos, get ready!"
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

        # Countdown in center
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

        # Reset run/session flags
        self.is_running_sequence = False

        # Reset payment / tap state for a NEW customer
        self.session_paid = False
        self._payment_in_progress = False
        self._welcome_tap_enabled = True

        # Reset subtitle text
        try:
            self.welcome_canvas.itemconfig(
                self.subtitle_item,
                text="Tap to pay & start"
            )
        except Exception:
            pass

        # Restart the subtle welcome animation (if you added it)
        try:
            self.start_welcome_pulse()
        except Exception:
            pass

    def _get_font_with_size(self, base_font, size: int):
        """
        Returns a font tuple with the same family/weight as base_font but a new size.
        Works for tuple-based fonts (most common in Tkinter apps).
        """
        if isinstance(base_font, tuple) and len(base_font) >= 2:
            family = base_font[0]
            rest = list(base_font[2:])  # weight/slant/etc
            return (family, size, *rest)
        return base_font  # fallback (won't resize if it's not a tuple)

    def start_welcome_pulse(self):
        """Start the pulsing animation on the welcome subtitle."""
        self.stop_welcome_pulse()  # ensure no duplicate loops
        self._welcome_pulse_on = True

        # Base Y position (so we can bob around it)
        self._welcome_subtitle_base_y = getattr(self, "_welcome_subtitle_base_y", None)
        if self._welcome_subtitle_base_y is None:
            # capture the current y as the base
            coords = self.welcome_canvas.coords(self.subtitle_item)
            if coords and len(coords) >= 2:
                self._welcome_subtitle_base_y = coords[1]
            else:
                self._welcome_subtitle_base_y = 300  # fallback

        # Pick a base size from your font_subtitle if possible
        base_size = 0
        if isinstance(self.font_subtitle, tuple) and len(self.font_subtitle) >= 2:
            base_size = int(self.font_subtitle[1])
        if base_size <= 0:
            base_size = 32  # fallback default

        self._welcome_pulse_state = 0  # 0..3 cycle

        def _tick():
            if not getattr(self, "_welcome_pulse_on", False):
                return

            # Cycle: bright -> dim -> bright -> dim
            cycle = self._welcome_pulse_state % 4

            # Subtle fade (white <-> light gray)
            fill = "white" if cycle in (0, 2) else "#d9d9d9"

            # Subtle bob (0px -> -3px -> 0px -> +3px)
            bob = 0
            if cycle == 1:
                bob = -3
            elif cycle == 3:
                bob = 3

            # Subtle size pulse (base -> base+1 -> base -> base+1)
            size = base_size if cycle in (0, 2) else (base_size + 1)
            font = self._get_font_with_size(self.font_subtitle, size)

            try:
                # Update text appearance
                self.welcome_canvas.itemconfig(self.subtitle_item, fill=fill, font=font)

                # Update position (keep x the same, bob y around base)
                x, _ = self.welcome_canvas.coords(self.subtitle_item)
                self.welcome_canvas.coords(self.subtitle_item, x, self._welcome_subtitle_base_y + bob)
            except Exception:
                # If the item doesn't exist yet, just stop gracefully
                self.stop_welcome_pulse()
                return

            self._welcome_pulse_state += 1
            self._welcome_pulse_after_id = self.root.after(450, _tick)  # adjust speed here

        self._welcome_pulse_after_id = self.root.after(0, _tick)

    def stop_welcome_pulse(self):
        """Stop the pulsing animation."""
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
        This function must only run ONCE per paid session.
        """

        # Must have a successful payment
        if not getattr(self, "session_paid", False):
            return

        # Must not already be running
        if getattr(self, "is_running_sequence", False):
            return

        # Lock the sequence so it cannot start twice
        self.is_running_sequence = True

        # ✅ TELEMETRY: session officially starts here (after payment, before capture begins)
        try:
            telemetry.session_started()
        except Exception:
            pass

        try:
            # OPTIONAL: stop welcome animations if you have them
            try:
                self.stop_welcome_pulse()
            except Exception:
                pass

            # Clear the welcome screen
            self.clear_screens()

            self.capture_frame.pack(expand=True, fill="both")

            # Initialize capture state
            self.photo_index = 1
            self.total_photos = getattr(self, "total_photos", 4)  # default to 4 if unset
            self.captured_files = []

            # Show splash text
            self.set_splash(True)

            # Small pause, then begin photo sequence
            self.root.after(1200, self.start_next_photo)

        except Exception as e:
            print("[ERROR] start_sequence failed:", e)
            self.is_running_sequence = False
            self.session_paid = False
            self.show_welcome_screen()

        except Exception as e:
            print("[ERROR] start_sequence failed:", e)

            # If something goes wrong, reset safely to welcome screen
            self.is_running_sequence = False
            self.session_paid = False
            self.show_welcome_screen()

    def start_next_photo(self):
        if self.photo_index > self.total_photos:
            self.finish_sequence()
            return

        self.set_splash(False)

        # Show photo label and countdown
        self.capture_canvas.itemconfig(self.photo_item, text=f"Photo {self.photo_index}", state="normal")
        self.capture_canvas.itemconfig(self.countdown_item, text="", state="normal")

        self.run_countdown(3)

    def run_countdown(self, n: int):
        if n <= 0:
            self.capture_canvas.itemconfig(self.countdown_item, text="", state="hidden")
            self.capture_photo()

            # Move to next photo after a short beat
            self.photo_index += 1
            self.root.after(600, self.start_next_photo)
            return

        self.capture_canvas.itemconfig(self.countdown_item, text=str(n), state="normal")
        self.root.after(1000, lambda: self.run_countdown(n - 1))

    def finish_sequence(self):
        self.is_running_sequence = False
        self.photo_index = 1  # reset defensively

        # Hide all capture overlays
        self.capture_canvas.itemconfig(self.photo_item, state="hidden")
        self.capture_canvas.itemconfig(self.countdown_item, state="hidden")
        self.capture_canvas.itemconfig(self.splash_item, state="hidden")

        # Small pause before moving to template picker (UX polish)
        self.root.after(400, self.show_template_picker_screen)

    def set_splash(self, visible: bool):
        self.capture_canvas.itemconfig(self.splash_item, state=("normal" if visible else "hidden"))
        if visible:
            self.capture_canvas.itemconfig(self.photo_item, state="hidden")
            self.capture_canvas.itemconfig(self.countdown_item, state="hidden")

    # -------------------------
    # Capture (placeholder)
    # -------------------------
    def capture_photo(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"photo_{self.photo_index}_{ts}.jpg"

        # Always save photos next to final.py (cross-platform safe)
        base_dir = os.path.dirname(__file__)
        filepath = os.path.join(base_dir, filename)

        if HAS_PREVIEW and self.latest_frame_bgr is not None:
            ok = cv2.imwrite(filepath, self.latest_frame_bgr)
            if ok:
                self.captured_files.append(filepath)  # ✅ FULL PATH
                print(f"[OK] Saved {filepath}")
            else:
                print(f"[WARN] Could not save {filepath}")
        else:
            # Simulation mode (no camera)
            self.captured_files.append(filepath)  # still full path
            print(f"[SIM] Captured {filepath} (no camera frame available)")

    # -------------------------
    # Camera preview loop
    # -------------------------
    def update_camera_frame(self):
        if HAS_PREVIEW and self.cap is not None:
            ret, frame = self.cap.read()
            if ret:
                self.latest_frame_bgr = frame

                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                w = self.root.winfo_width()
                h = self.root.winfo_height()
                if w > 1 and h > 1:
                    frame_rgb = cv2.resize(frame_rgb, (w, h), interpolation=cv2.INTER_AREA)

                img = Image.fromarray(frame_rgb)
                self.tk_preview_img = ImageTk.PhotoImage(img)  # keep reference

                # Put preview on capture canvas (only matters when capture screen is visible)
                self.capture_canvas.itemconfig(self.bg_image_item, image=self.tk_preview_img)
                self.capture_canvas.tag_lower(self.bg_image_item)

        # ~30 FPS
        self.root.after(33, self.update_camera_frame)

    # -------------------------
    # Cleanup
    # -------------------------
    def build_template_picker_screen(self):
        self.template_frame = tk.Frame(self.container, bg="black")

        self.template_canvas = tk.Canvas(
            self.template_frame,
            bg="black",
            highlightthickness=0,
            borderwidth=0
        )
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

        # A status/message text (used for "Printing your photos...", "Done", etc.)
        self.template_status_item = self.template_canvas.create_text(
            self.root.winfo_screenwidth() // 2,
            self.root.winfo_screenheight() // 2,
            text="",
            fill="black",
            font=self.font_title,
            anchor="center",
            state="hidden"
        )

        # Preview of composed strip (hidden until a template is selected)
        self.template_preview_item = self.template_canvas.create_image(
            self.root.winfo_screenwidth() // 2,
            int(self.root.winfo_screenheight() * 0.55),
            anchor="center",
            image=None
        )

        # Keep references to thumbnails so they don't get garbage-collected
        self._template_thumb_refs = []
        self._template_item_to_path = {}
        self._template_grid_item_ids = []  # canvas item ids for template thumbnails

    def _enable_touch_scroll(self):
        # Drag the canvas content with finger/mouse
        self.template_canvas.bind("<ButtonPress-1>", self._start_touch_scroll)
        self.template_canvas.bind("<B1-Motion>", self._do_touch_scroll)

    def _start_touch_scroll(self, event):
        # Remember the starting point
        self.template_canvas.scan_mark(event.x, event.y)

    def _do_touch_scroll(self, event):
        # Drag to scroll
        self.template_canvas.scan_dragto(event.x, event.y, gain=1)
    def show_template_picker_screen(self):
        self.clear_screens()
        self.template_frame.pack(expand=True, fill="both")

        # Hide status + preview initially
        self.template_canvas.itemconfig(self.template_status_item, state="hidden", text="")
        self.template_canvas.itemconfig(self.template_preview_item, image=None, state="hidden")
        self._composed_preview_ref = None
        self.last_composed_strip = None

        self.render_template_grid()
        self._enable_touch_scroll()

    def render_template_grid(self):
        # --- Clear old thumbnail canvas items ---
        for item_id in self._template_grid_item_ids:
            try:
                self.template_canvas.delete(item_id)
            except Exception:
                pass

        self._template_grid_item_ids.clear()
        self._template_thumb_refs.clear()

        # Hide/clear any prior status message
        self.template_canvas.itemconfig(self.template_status_item, state="hidden", text="")

        # --- Load templates (expects PNG+JSON pairs) ---
        try:
            self.templates = load_templates(self.templates_dir)  # List[TemplateDef]
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

        # --- Grid layout ---
        screen_w = self.root.winfo_screenwidth()
        start_y = 140
        pad = 30
        cols = 3

        thumb_w = int((screen_w - pad * (cols + 1)) / cols)
        thumb_h = int(thumb_w * 1.2)

        # --- Create thumbnails ---
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
                x, y,
                image=tk_img,
                anchor="center"
            )
            self._template_grid_item_ids.append(item_id)

            # Click -> select this TemplateDef
            self.template_canvas.tag_bind(
                item_id,
                "<Button-1>",
                lambda e, td=tdef: self.on_template_selected(td)
            )

        # --- Update scroll region so user can scroll through ALL templates ---
        self.template_canvas.update_idletasks()
        bbox = self.template_canvas.bbox("all")
        if bbox:
            self.template_canvas.configure(scrollregion=bbox)
        else:
            self.template_canvas.configure(scrollregion=(0, 0, 0, 0))

        self.template_canvas.update_idletasks()
        bbox = self.template_canvas.bbox("all")
        if bbox:
            self.template_canvas.configure(scrollregion=bbox)
    def on_template_selected(self, tdef: TemplateDef):
        # Guard: ensure we have 4 photos (or your total_photos)
        needed = getattr(self, "total_photos", 4)
        if len(self.captured_files) < needed:
            self.template_canvas.itemconfig(
                self.template_status_item,
                state="normal",
                text=f"Missing photos ({len(self.captured_files)}/{needed})."
            )
            self.template_canvas.tag_raise(self.template_status_item)
            return

        # ✅ TELEMETRY: template chosen (daily + lifetime)
        try:
            # Use a stable identifier (prefer an explicit id/name; fallback to file stem)
            template_id = getattr(tdef, "template_id", None) or getattr(tdef, "name", None) or tdef.png_path.stem
            telemetry.template_chosen(str(template_id))
        except Exception:
            pass

        # Hide template thumbnails so only preview remains
        self.hide_template_grid()
        self.template_canvas.itemconfig(self.template_title_item, state="hidden")
        self.template_canvas.itemconfig(self.template_status_item, state="hidden", text="")

        # Compose using JSON-defined slots
        try:
            composed_img = compose(tdef.png_path, tdef.slots, self.captured_files[:needed])
        except Exception as e:
            self.template_canvas.itemconfig(
                self.template_status_item,
                state="normal",
                text=f"Compose error:\n{e}"
            )
            self.template_canvas.tag_raise(self.template_status_item)
            return

        # Save composed strip to a file (so we can print it)
        base_dir = os.path.dirname(__file__)
        out_name = f"strip_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        # --- Create doubled strip ---
        double_img = side_by_side(composed_img)

        # --- Save doubled strip (print-ready) ---
        base_dir = os.path.dirname(__file__)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_name = f"strip_double_{ts}.png"
        out_path = os.path.join(base_dir, out_name)

        double_img.convert("RGB").save(out_path, quality=95)
        self.last_composed_strip = out_path

        print("[OK] Saved doubled strip:", out_path)

        # --- 3. Send to printer (Windows PC) ---
        try:
            print_image_windows(self.last_composed_strip, self.printer_name)
        except Exception as e:
            print("[WARN] Printing failed:", e)

        self.template_canvas.itemconfig(self.template_preview_item, state="hidden")

        # Printing overlay
        self.template_canvas.itemconfig(
            self.template_status_item,
            state="normal",
            text="Printing your photos!"
        )
        self.template_canvas.tag_raise(self.template_status_item)

        # Print then return
        self.root.after(5000, lambda: self._print_then_done(out_path))

    def show_printing_message(self):
        self.template_canvas.itemconfig(
            self.template_status_item,
            state="normal",
            text="Printing your photos..."
        )
        self.template_canvas.tag_raise(self.template_status_item)

    def _print_then_done(self, composed_path: str):
        # Try to print
        ok, details = self.print_file(composed_path)

        # ✅ TELEMETRY: count prints (lifetime + daily) + paper roll warnings/reset
        try:
            telemetry.print_result(ok=ok, details=details)
        except Exception:
            pass

        if not ok:
            self.template_canvas.itemconfig(
                self.template_status_item,
                state="normal",
                text="Print failed.\nReturning to start."
            )
            self.root.after(4000, self.cleanup_session_files_and_return)
            return

        self.template_canvas.itemconfig(self.template_status_item, state="normal", text="Done!")
        self.root.after(2000, self.cleanup_session_files_and_return)

    def print_file(self, file_path: str) -> tuple[bool, str]:
        """
        macOS print via `lp`.
        If you set self.printer_name, it will target that printer.

        Returns:
            (ok, details)
            - ok: True if the job was queued successfully
            - details: stderr/stdout or exception string (trimmed), useful for telemetry
        """
        try:
            cmd = ["lp"]
            if self.printer_name:
                cmd += ["-d", self.printer_name]
            cmd.append(file_path)

            # This queues the job; it does not guarantee paper came out.
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)

            details = (result.stdout or "").strip()
            return True, details[:400]  # keep it short

        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip() if hasattr(e, "stderr") else ""
            stdout = (e.stdout or "").strip() if hasattr(e, "stdout") else ""
            details = (stderr or stdout or str(e)).strip()
            print(f"[PRINT ERROR] {details}")
            return False, details[:400]

        except Exception as e:
            details = str(e).strip()
            print(f"[PRINT ERROR] {details}")
            return False, details[:400]

    def cleanup_session_files_and_return(self):
        base_dir = os.path.dirname(__file__)

        def _abs(p: str) -> str:
            if not p:
                return p
            p = str(p)
            return p if os.path.isabs(p) else os.path.join(base_dir, p)

        deleted = 0

        # --- Delete captured originals ---
        for f in list(getattr(self, "captured_files", []) or []):
            try:
                f_abs = _abs(f)
                print("[CLEANUP] trying delete:", f_abs)

                if f_abs and os.path.exists(f_abs):
                    os.remove(f_abs)
                    deleted += 1
                    print("[CLEANUP] deleted:", f_abs)
                else:
                    print("[CLEANUP] not found:", f_abs)

            except Exception as e:
                print(f"[WARN] Could not delete {f}: {e}")

        # --- Optional: delete the composed strip too ---
        last_strip = getattr(self, "last_composed_strip", None)
        if last_strip:
            try:
                strip_abs = _abs(last_strip)
                print("[CLEANUP] trying delete strip:", strip_abs)

                if os.path.exists(strip_abs):
                    os.remove(strip_abs)
                    deleted += 1
                    print("[CLEANUP] deleted strip:", strip_abs)
                else:
                    print("[CLEANUP] strip not found:", strip_abs)

            except Exception as e:
                print(f"[WARN] Could not delete composed strip: {e}")

        # --- Reset session state ---
        self.captured_files = []
        self.last_composed_strip = None
        self._payment_in_progress = False

        print(f"[CLEANUP] done. deleted {deleted} files.")

        # --- Return to welcome screen ---
        self.show_welcome_screen()

    def hide_template_grid(self):
        print("HIDING TEMPLATE COUNT:", len(self._template_grid_item_ids))
        for item_id in self._template_grid_item_ids:
            try:
                self.template_canvas.itemconfig(item_id, state="hidden")
            except Exception:
                pass

    def cleanup_and_exit(self):
        try:
            if HAS_PREVIEW and self.cap is not None:
                self.cap.release()
        finally:
            self.root.destroy()

def main():
    templates = load_templates(TEMPLATES_DIR)

    root = tk.Tk()

    # Heartbeat every 10 minutes
    def schedule_heartbeat():
        try:
            telemetry.heartbeat(uptime_start=START_TIME)
        except Exception:
            pass
        root.after(10 * 60 * 1000, schedule_heartbeat)

    schedule_heartbeat()

    # Build UI (pass templates if your UI needs them)
    PhotoBoothUI(root)  # or PhotoBoothUI(root, templates)

    # Window close handler
    def on_close():
        try:
            telemetry.log_event("booth_stopped")
        except Exception:
            pass

        try:
            telemetry.send_telegram(f"🛑 Booth stopped ({telemetry.booth_id})")
        except Exception:
            pass

        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    # ✅ KEEP THE APP RUNNING
    root.mainloop()




if __name__ == "__main__":
    main()
