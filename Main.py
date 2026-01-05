import sys
import os
import time
from PyQt5.QtWidgets import QApplication, QWidget, QLabel, QPushButton, QVBoxLayout
from PyQt5.QtCore import QTimer
import cv2

# -----------------------------
# CONFIG
# -----------------------------
PHOTO_SAVE_DIR = os.path.expanduser("~/PhotoboothPhotos")
if not os.path.exists(PHOTO_SAVE_DIR):
    os.makedirs(PHOTO_SAVE_DIR)

# Simulate payment trigger
paid = False

# -----------------------------
# UI CLASS
# -----------------------------
class PhotoBoothApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Photobooth Test")
        self.setGeometry(100, 100, 800, 600)

        self.layout = QVBoxLayout()
        self.label = QLabel("LOCKED: Please pay to start")
        self.label.setStyleSheet("font-size: 32px; color: red;")
        self.layout.addWidget(self.label)

        self.start_button = QPushButton("Simulate Payment")
        self.start_button.clicked.connect(self.simulate_payment)
        self.layout.addWidget(self.start_button)

        self.setLayout(self.layout)

        self.countdown_timer = QTimer()
        self.countdown_timer.timeout.connect(self.update_countdown)
        self.countdown_value = 3

    def simulate_payment(self):
        global paid
        paid = True
        self.start_photobooth()

    def start_photobooth(self):
        if paid:
            self.label.setText("READY! Starting countdown...")
            self.label.setStyleSheet("font-size: 32px; color: green;")
            self.countdown_value = 3
            self.countdown_timer.start(1000)  # 1-second intervals

    def update_countdown(self):
        if self.countdown_value > 0:
            self.label.setText(f"Smile! {self.countdown_value}")
            self.countdown_value -= 1
        else:
            self.countdown_timer.stop()
            self.capture_photo()

    def capture_photo(self):
        # Try to use camera if available, otherwise save a placeholder image
        try:
            cap = cv2.VideoCapture(0)  # Use built-in webcam
            ret, frame = cap.read()
            if ret:
                filename = os.path.join(PHOTO_SAVE_DIR, f"photo_{int(time.time())}.jpg")
                cv2.imwrite(filename, frame)
                self.label.setText(f"Photo saved: {filename}")
            cap.release()
        except:
            # Fallback: create a blank placeholder
            filename = os.path.join(PHOTO_SAVE_DIR, f"photo_{int(time.time())}_placeholder.jpg")
            cv2.imwrite(filename, 255 * np.ones((480,640,3), dtype=np.uint8))
            self.label.setText(f"Placeholder saved: {filename}")

        # Reset to LOCKED state
        global paid
        paid = False
        self.label.setText("LOCKED: Please pay to start")
        self.label.setStyleSheet("font-size: 32px; color: red;")

# -----------------------------
# RUN APP
# -----------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = PhotoBoothApp()
    window.show()
    sys.exit(app.exec_())
