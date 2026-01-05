import cv2
import os
import time
import numpy as np
import subprocess
from tkinter import Tk, Button, Label
from PIL import Image, ImageTk

# -----------------------------
# Settings
# -----------------------------
PHOTO_SAVE_DIR = os.path.expanduser("~/Desktop/PhotoboothPhotos")
TEMPLATE_DIR = os.path.expanduser("~/Desktop/PhotoboothTemplates")
if not os.path.exists(PHOTO_SAVE_DIR):
    os.makedirs(PHOTO_SAVE_DIR)

CAMERA_INDEX = 0
BRIGHTNESS_ALPHA = 1.5
BRIGHTNESS_BETA = 50

# Define template slots (x, y, width, height)
TEMPLATE_SLOTS = [
    (20, 20, 667, 355),
    (20, 395, 667, 355),
    (20, 770, 667, 355),
    (20, 1145, 667, 355)
]

template_paths = [
    os.path.join(TEMPLATE_DIR, "template1.png"),
    os.path.join(TEMPLATE_DIR, "template2.png")
]

# -----------------------------
# Functions
# -----------------------------
def choose_template():
    """
        Opens a Tkinter window showing previews of the templates
        and lets the user click one to choose it.
        Returns the chosen template image (OpenCV) and its file path.
        """

    root = Tk()
    root.title("Choose Your Photobooth Template")

    chosen_path = {"path": None}  # mutable so nested function can set it

    # Load and resize previews for GUI
    previews = []
    for path in template_paths:
        img = Image.open(path)
        img = img.resize((300, 450))  # Adjust preview size if needed
        previews.append(ImageTk.PhotoImage(img))

    # ------- Template 1 -------
    label1 = Label(root, image=previews[0])
    label1.grid(row=0, column=0, padx=20, pady=10)

    def choose1():
        chosen_path["path"] = template_paths[0]
        root.destroy()

    button1 = Button(root, text="Choose Template 1", command=choose1)
    button1.grid(row=1, column=0, pady=10)

    # ------- Template 2 -------
    label2 = Label(root, image=previews[1])
    label2.grid(row=0, column=1, padx=20, pady=10)

    def choose2():
        chosen_path["path"] = template_paths[1]
        root.destroy()

    button2 = Button(root, text="Choose Template 2", command=choose2)
    button2.grid(row=1, column=1, pady=10)

    root.mainloop()

    if chosen_path["path"] is None:
        raise Exception("No template selected!")

    # Return actual OpenCV image + path
    chosen_cv2 = cv2.imread(chosen_path["path"])
    return chosen_cv2, chosen_path["path"]

def capture_photo_live(slot_num):
    cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        print("Camera failed to open!")
        return None

    # Live preview with countdown
    for count in range(3, 0, -1):
        ret, frame = cap.read()
        if not ret:
            print("Failed to capture frame during countdown.")
            cap.release()
            return None

        # Brighten frame
        frame = frame.astype(np.float32)
        frame = frame * BRIGHTNESS_ALPHA + BRIGHTNESS_BETA
        frame = np.clip(frame, 0, 255).astype(np.uint8)

        # Overlay countdown
        cv2.putText(frame, f"{count}", (50, 100), cv2.FONT_HERSHEY_SIMPLEX,
                    4, (0, 0, 255), 8, cv2.LINE_AA)

        cv2.imshow("Photobooth Preview", frame)
        cv2.waitKey(1000)  # wait 1 second

    # Capture final frame
    ret, frame = cap.read()
    cap.release()
    if not ret:
        print("Failed to capture final photo.")
        return None

    frame = frame.astype(np.float32)
    frame = frame * BRIGHTNESS_ALPHA + BRIGHTNESS_BETA
    frame = np.clip(frame, 0, 255).astype(np.uint8)

    cv2.destroyWindow("Photobooth Preview")
    return frame

def resize_and_place(photo, template, slot):
    x, y, w, h = slot
    resized = cv2.resize(photo, (w, h))
    template[y:y+h, x:x+w] = resized

# -----------------------------
# Main Program
# -----------------------------
if __name__ == "__main__":
    template_img, template_path = choose_template()
    output_img = template_img.copy()
    print("Template size:", template_img.shape)
    print("4 photos coming your way!")
    time.sleep(2)

    for i in range(4):
        print(f"\nGet ready for photo {i+1}!")
        photo = capture_photo_live(i+1)
        if photo is not None:
            resize_and_place(photo, output_img, TEMPLATE_SLOTS[i])
        else:
            print("Skipping this photo...")

    # Save final composite
    filename = os.path.join(PHOTO_SAVE_DIR, f"photobooth_{int(time.time())}.jpg")
    cv2.imwrite(filename, output_img)
    print(f"\nFinal photobooth image saved: {filename}")
    subprocess.run(["open", filename])
