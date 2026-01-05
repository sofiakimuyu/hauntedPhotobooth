import json
import os
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageTk


# Folder that contains templates (PNGs + JSONs)
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "PhotoboothTemplates")

# Sample photos (change extensions if you want: .png, .jpeg, .webp, etc.)
SAMPLE_PHOTOS = [
    os.path.join(os.path.dirname(__file__), "samplephoto1.jpg"),
    os.path.join(os.path.dirname(__file__), "samplephoto2.jpg"),
    os.path.join(os.path.dirname(__file__), "samplephoto3.jpg"),
    os.path.join(os.path.dirname(__file__), "samplephoto4.jpg"),
]


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
    if len(photo_paths) != 4:
        raise ValueError("Expected exactly 4 photo paths.")

    # Ensure slot order is 1..4
    slots_sorted = sorted(slots, key=lambda s: s.id)
    if len(slots_sorted) != 4:
        raise ValueError("Template JSON must define exactly 4 slots.")

    for slot, photo_path in zip(slots_sorted, photo_paths):
        img = Image.open(photo_path).convert("RGBA")
        if slot.rotation:
            img = img.rotate(slot.rotation, expand=True)
        fitted = cover_resize(img, slot.w, slot.h)
        base.paste(fitted, (slot.x, slot.y), fitted)

    return base


def load_templates(dir_path: str) -> List[TemplateDef]:
    if not os.path.isdir(dir_path):
        raise FileNotFoundError(f"Missing folder: {dir_path}")

    files = os.listdir(dir_path)
    pngs = {os.path.splitext(f)[0]: os.path.join(dir_path, f) for f in files if f.lower().endswith(".png")}
    jsons = {os.path.splitext(f)[0]: os.path.join(dir_path, f) for f in files if f.lower().endswith(".json")}

    keys = sorted(set(pngs.keys()) & set(jsons.keys()))
    if not keys:
        raise FileNotFoundError(
            f"No templates found in {dir_path}.\n"
            f"Add matching pairs like: mytemplate.png + mytemplate.json"
        )

    templates: List[TemplateDef] = []
    for key in keys:
        png_path = pngs[key]
        json_path = jsons[key]

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        name = data.get("name", key)

        slots_raw = data.get("slots", [])
        if len(slots_raw) != 4:
            raise ValueError(f"{json_path} must have exactly 4 slots (has {len(slots_raw)}).")

        defaults = data.get("defaults", {})

        slots = []
        for s in data["slots"]:
            merged = {**defaults, **s}  # slot overrides defaults if it sets a value

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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Template Preview Tester")
        self.geometry("1100x750")

        self.templates: List[TemplateDef] = []
        self.thumb_refs: Dict[str, ImageTk.PhotoImage] = {}
        self.result_ref: Optional[ImageTk.PhotoImage] = None
        self._latest_composed: Optional[Image.Image] = None

        self._build_ui()
        self._validate_sample_photos()
        self._load_gallery()

    def _build_ui(self):
        # Two "screens": gallery + result
        self.gallery_frame = tk.Frame(self)
        self.result_frame = tk.Frame(self)

        # -------- Gallery screen --------
        top = tk.Frame(self.gallery_frame)
        top.pack(fill="x", padx=12, pady=12)

        tk.Label(top, text="Choose your template", font=("Helvetica", 20, "bold")).pack(anchor="w")
        tk.Label(top, text=f"Loading templates from: {TEMPLATES_DIR}", fg="gray").pack(anchor="w", pady=(4, 0))

        # scrollable gallery
        container = tk.Frame(self.gallery_frame)
        container.pack(fill="both", expand=True, padx=12, pady=12)

        self.canvas = tk.Canvas(container, bg="#f2f2f2")
        self.scrollbar = tk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.gallery_inner = tk.Frame(self.canvas, bg="#f2f2f2")
        self.canvas.create_window((0, 0), window=self.gallery_inner, anchor="nw")
        self.gallery_inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))

        # -------- Result screen --------
        rtop = tk.Frame(self.result_frame)
        rtop.pack(fill="x", padx=12, pady=12)

        tk.Button(rtop, text="← Back to templates", command=self._show_gallery).pack(side="left")
        tk.Label(rtop, text="Composed Preview", font=("Helvetica", 18, "bold")).pack(side="left", padx=12)

        self.result_label = tk.Label(self.result_frame, bd=2, relief="groove")
        self.result_label.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        # Show gallery first
        self.gallery_frame.pack(fill="both", expand=True)

    def _validate_sample_photos(self):
        missing = [p for p in SAMPLE_PHOTOS if not os.path.exists(p)]
        if missing:
            msg = "Missing sample photos:\n\n" + "\n".join(missing) + "\n\n" \
                  "Put samplephoto1.jpg..samplephoto4.jpg next to the script, or update SAMPLE_PHOTOS in code."
            messagebox.showerror("Missing sample photos", msg)
            # Don't exit hard; still allow seeing templates
        return

    def _load_gallery(self):
        # Clear old
        for w in self.gallery_inner.winfo_children():
            w.destroy()
        self.thumb_refs.clear()

        try:
            self.templates = load_templates(TEMPLATES_DIR)
        except Exception as e:
            messagebox.showerror("Template load error", str(e))
            self.templates = []
            return

        # Layout in a grid
        cols = 3
        thumb_w = 320

        for idx, tdef in enumerate(self.templates):
            r = idx // cols
            c = idx % cols

            card = tk.Frame(self.gallery_inner, bg="white", bd=1, relief="solid")
            card.grid(row=r, column=c, padx=10, pady=10, sticky="n")

            # thumbnail
            img = Image.open(tdef.png_path).convert("RGBA")
            scale = thumb_w / img.size[0]
            thumb_h = int(img.size[1] * scale)
            thumb = img.resize((thumb_w, thumb_h), Image.LANCZOS)

            imgtk = ImageTk.PhotoImage(thumb)
            self.thumb_refs[tdef.key] = imgtk

            btn = tk.Label(card, image=imgtk, cursor="hand2", bg="white")
            btn.pack(padx=8, pady=8)
            btn.bind("<Button-1>", lambda e, td=tdef: self._on_template_click(td))

            tk.Label(card, text=tdef.name, bg="white", font=("Helvetica", 12, "bold")).pack(padx=8)
            tk.Label(card, text=f"{tdef.size[0]}×{tdef.size[1]}", bg="white", fg="gray").pack(padx=8, pady=(0, 8))

            # make whole card clickable (title area too)
            card.bind("<Button-1>", lambda e, td=tdef: self._on_template_click(td))

        # add some spacing at bottom
        self.gallery_inner.grid_columnconfigure(0, weight=1)
        self.gallery_inner.grid_columnconfigure(1, weight=1)
        self.gallery_inner.grid_columnconfigure(2, weight=1)

    def _on_template_click(self, tdef: TemplateDef):
        # Ensure sample photos exist
        if any(not os.path.exists(p) for p in SAMPLE_PHOTOS):
            self._validate_sample_photos()
            if any(not os.path.exists(p) for p in SAMPLE_PHOTOS):
                return

        try:
            composed = compose(tdef.png_path, tdef.slots, SAMPLE_PHOTOS)
        except Exception as e:
            messagebox.showerror("Compose error", str(e))
            return

        self._latest_composed = composed
        self._show_result(composed)

    def _show_result(self, img: Image.Image):
        # switch frames
        self.gallery_frame.pack_forget()
        self.result_frame.pack(fill="both", expand=True)

        self.update_idletasks()
        max_w = max(200, self.result_label.winfo_width())
        max_h = max(200, self.result_label.winfo_height())

        w, h = img.size
        scale = min(max_w / w, max_h / h)
        disp = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        self.result_ref = ImageTk.PhotoImage(disp)
        self.result_label.configure(image=self.result_ref)

    def _show_gallery(self):
        self.result_frame.pack_forget()
        self.gallery_frame.pack(fill="both", expand=True)


if __name__ == "__main__":
    app = App()
    app.mainloop()
