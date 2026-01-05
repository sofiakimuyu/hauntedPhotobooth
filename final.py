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
import os
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

# Optional preview stack
try:
    import cv2
    from PIL import Image, ImageTk
    HAS_PREVIEW = True
except Exception:
    HAS_PREVIEW = False
import os
import glob
import subprocess

from dataclasses import dataclass
from typing import List, Tuple

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

def load_templates(dir_path: str) -> list[TemplateDef]:
    if not os.path.isdir(dir_path):
        raise FileNotFoundError(f"Missing folder: {dir_path}")

    files = os.listdir(dir_path)
    pngs = {os.path.splitext(f)[0]: os.path.join(dir_path, f)
            for f in files if f.lower().endswith(".png")}
    jsons = {os.path.splitext(f)[0]: os.path.join(dir_path, f)
             for f in files if f.lower().endswith(".json")}

    keys = sorted(set(pngs.keys()) & set(jsons.keys()))
    if not keys:
        raise FileNotFoundError(
            f"No templates found in {dir_path}.\n"
            f"Add matching pairs like: mytemplate.png + mytemplate.json"
        )

    templates = []
    for key in keys:
        png_path = pngs[key]
        json_path = jsons[key]

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        name = data.get("name", key)
        slots_raw = data.get("slots", [])

        if len(slots_raw) != 4:
            raise ValueError(f"{json_path} must define exactly 4 slots.")

        defaults = data.get("defaults", {})
        slots = []

        for s in slots_raw:
            merged = {**defaults, **s}
            slots.append(Slot(
                id=int(merged["id"]),
                x=int(merged["x"]),
                y=int(merged["y"]),
                w=int(merged["w"]),
                h=int(merged["h"]),
                rotation=int(merged.get("rotation", 0))
            ))

        w, h = Image.open(png_path).size

        templates.append(TemplateDef(
            key=key,
            name=name,
            png_path=png_path,
            json_path=json_path,
            slots=slots,
            size=(w, h),
        ))

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
    """Compose the template with photos in slot-id order."""
    base = Image.open(template_png_path).convert("RGBA")

    # Expect exactly 4 photos for now
    if len(photo_paths) != 4:
        raise ValueError(f"Expected exactly 4 photo paths (got {len(photo_paths)}).")

    # Ensure slot order is 1..4
    slots_sorted = sorted(slots, key=lambda s: s.id)
    if len(slots_sorted) != 4:
        raise ValueError(f"Template JSON must define exactly 4 slots (got {len(slots_sorted)}).")

    for slot, photo_path in zip(slots_sorted, photo_paths):
        img = Image.open(photo_path).convert("RGBA")
        if slot.rotation:
            img = img.rotate(slot.rotation, expand=True)
        fitted = cover_resize(img, slot.w, slot.h)
        base.paste(fitted, (slot.x, slot.y), fitted)

    return base

class PhotoBoothUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PhotoBooth")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="black")
        self.root.bind("<Escape>", lambda e: self.cleanup_and_exit())
        self.templates = []
        # ---- state ----
        self.paid = False
        self.is_running_sequence = False
        self.photo_index = 1  # 1..3
        self.total_photos = 4

        # ---- template / printing state ----
        self.captured_files = []          # stores the 3 photo filenames from a session
        self.templates_dir = os.path.join(os.path.dirname(__file__), "PhotoboothTemplates") # folder containing photo strip templates
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
        self.welcome_bg_path = "stanfordwelcomebackground.jpg"
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

    # -------------------------
    # Screen builders (Canvas only)
    # -------------------------
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
        if self.welcome_bg_path and HAS_PREVIEW:
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

        if self.welcome_bg_path:
            img = Image.open(self.welcome_bg_path)
            img = img.resize(
                (self.root.winfo_screenwidth(), self.root.winfo_screenheight()),
                Image.LANCZOS
            )
            self.tk_welcome_bg = ImageTk.PhotoImage(img)

        self.welcome_bg_item = self.welcome_canvas.create_image(
            0, 0,
            anchor="nw",
            image=self.tk_welcome_bg
        )
        self.welcome_canvas.tag_lower(self.welcome_bg_item)

        # Title text (Canvas text = no background boxes)
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
            text="Pay to start",
            fill="white",
            font=self.font_subtitle,
            anchor="center"
        )

        # Buttons are real widgets, placed onto Canvas with create_window
        self.pay_button = tk.Button(
            self.welcome_frame,  # parent can be frame; Canvas will host it
            text="Pay (demo)",
            font=self.font_button,
            width=18,
            height=2,
            command=self.simulate_payment,
            bg="white",
            fg="black",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
            activebackground="#e6e6e6",
        )

        self.start_button = tk.Button(
            self.welcome_frame,
            text="Start",
            font=self.font_button,
            width=18,
            height=2,
            command=self.start_sequence,
            bg="white",
            fg="black",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            takefocus=False,
            activebackground="#e6e6e6",
        )

        # Place buttons (we'll hide/show Start by canvas item state)
        cx = self.root.winfo_screenwidth() // 2
        self.pay_button_win = self.welcome_canvas.create_window(
            cx, 430,
            window=self.pay_button,
            anchor="center"
        )
        self.start_button_win = self.welcome_canvas.create_window(
            cx, 560,
            window=self.start_button,
            anchor="center",
            state="hidden"  # locked until paid
        )

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
        for child in self.container.winfo_children():
            child.pack_forget()

    def show_welcome_screen(self):
        self.clear_screens()
        self.welcome_frame.pack(expand=True, fill="both")
        self.is_running_sequence = False

        # Ensure correct button visibility
        if self.paid:
            self.welcome_canvas.itemconfig(self.start_button_win, state="normal")
        else:
            self.welcome_canvas.itemconfig(self.start_button_win, state="hidden")

    def show_capture_screen(self):
        self.clear_screens()
        self.capture_frame.pack(expand=True, fill="both")

    # -------------------------
    # Payment + flow
    # -------------------------
    def simulate_payment(self):
        self.paid = True
        self.pay_button.config(state="disabled", text="Paid")
        self.welcome_canvas.itemconfig(self.start_button_win, state="normal")

    def start_sequence(self):
        if not self.paid or self.is_running_sequence:
            return

        self.is_running_sequence = True
        self.photo_index = 1
        self.captured_files = []
        self.show_capture_screen()

        # Splash for 2 seconds, then start Photo 1 countdown
        self.set_splash(True)
        self.root.after(2000, self.start_next_photo)

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

        if HAS_PREVIEW and self.latest_frame_bgr is not None:
            ok = cv2.imwrite(filename, self.latest_frame_bgr)
            if ok:
                self.captured_files.append(filename)  # ✅ append ONLY after success
                print(f"[OK] Saved {filename}")
            else:
                print(f"[WARN] Could not save {filename}")
        else:
            # Simulation mode (no camera)
            self.captured_files.append(filename)  # still record it
            print(f"[SIM] Captured {filename} (no camera frame available)")

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
        except Exception as e:
            self.template_canvas.itemconfig(
                self.template_status_item,
                state="normal",
                text=f"Template load error:\n{e}"
            )
            self.template_canvas.tag_raise(self.template_status_item)
            self.templates = []
            # No need to scroll if nothing is shown
            self.template_canvas.configure(scrollregion=(0, 0, 0, 0))
            return

        if not self.templates:
            self.template_canvas.itemconfig(
                self.template_status_item,
                state="normal",
                text=f"No templates found in:\n{self.templates_dir}"
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
        out_path = f"strip_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        composed_img.convert("RGB").save(out_path, quality=95)
        self.last_composed_strip = out_path

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
        ok = self.print_file(composed_path)
        if not ok:
            self.template_canvas.itemconfig(self.template_status_item, state="normal",
                                            text="Print failed.\nReturning to start.")
            self.root.after(4000, self.cleanup_session_files_and_return)
            return

        self.template_canvas.itemconfig(self.template_status_item, state="normal", text="Done!")
        self.root.after(2000, self.cleanup_session_files_and_return)

    def print_file(self, file_path: str) -> bool:
        """
        macOS print via `lp`.
        If you set self.printer_name, it will target that printer.
        """
        try:
            cmd = ["lp"]
            if self.printer_name:
                cmd += ["-d", self.printer_name]
            cmd.append(file_path)

            # This queues the job; it does not guarantee paper came out.
            subprocess.run(cmd, check=True, capture_output=True)
            return True
        except Exception as e:
            print(f"[PRINT ERROR] {e}")
            return False

    def cleanup_session_files_and_return(self):
        # Delete captured originals
        for f in self.captured_files:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception as e:
                print(f"[WARN] Could not delete {f}: {e}")

        # Optional: delete the composed strip too
        if self.last_composed_strip:
            try:
                if os.path.exists(self.last_composed_strip):
                    os.remove(self.last_composed_strip)
            except Exception as e:
                print(f"[WARN] Could not delete composed strip: {e}")

        # Reset session state
        self.captured_files = []
        self.last_composed_strip = None
        self.paid = False

        # Reset welcome buttons to initial state
        self.pay_button.config(state="normal", text="Pay (demo)")
        self.welcome_canvas.itemconfig(self.start_button_win, state="hidden")

        self.show_welcome_screen()
        self.root.update_idletasks()
        self.root.update()

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
    root = tk.Tk()
    PhotoBoothUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
