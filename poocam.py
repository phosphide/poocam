#!/usr/bin/env python3
import datetime
import io
import os
import subprocess
from threading import Thread

import click
import numpy as np
import time
import queue
import logging
import signal

from PyQt5.QtCore import Qt, QSize, QEvent, qDebug, QTimer
from PyQt5.QtWidgets import QApplication, QMainWindow, QWidget, QLabel, QPushButton, \
    QScrollArea, QFrame
from PyQt5.QtGui import QCursor, QImage, QPainter, QColor
from picamera2 import Picamera2
from picamera2.previews.qt import QGlPicamera2
from picamera2.encoders import H264Encoder
from picamera2.outputs import FileOutput
from libcamera import controls


# TODO: preview enabled timeout, external triggers, streaming, extra controls


poocam_config = {
    "video_width": 1280,
    "video_height": 720,
    "low_res_width": 160,
    "low_res_height": 120,
    "cut_left": 0.15,
    "cut_right": 0.20,
    "recording_timeout": 3,
    "recording_bitrate": 6_000_000,
}


def set_brightness(brightness: float):
    with open("/sys/waveshare/rpi_backlight/brightness", "w+b") as brightness_device:
        brightness_int = min(max(int((1 - brightness) * 255), 0), 255)
        brightness_device.write(f"{brightness_int}".encode())


class PoocamMainWindow(QMainWindow):
    def __init__(self, temp_directory: str, target_directory: str, mse_threshold: float, screen_timeout: float):
        super().__init__()
        self._logger = logging.getLogger("Poocam")
        self.setWindowTitle("Poocam")

        self.temp_directory = temp_directory
        self.target_directory = target_directory
        self.mse_threshold = mse_threshold
        self.current_filename: str | None = None

        self.screen_timeout = screen_timeout
        self.sleep_timer = QTimer(self)
        self.sleep_timer.setSingleShot(True)
        self.sleep_timer.timeout.connect(self.sleep)

        self.camera_scroll_widget = QScrollArea()
        self.camera_scroll_widget.setFrameShape(QFrame.NoFrame)
        self.camera_scroll_widget.horizontalScrollBar().setStyleSheet("QScrollBar {height:0px;}")
        self.camera_scroll_widget.verticalScrollBar().setStyleSheet("QScrollBar {width:0px;}")
        self.setCentralWidget(self.camera_scroll_widget)

        self.camera: Picamera2 | None = None
        self.camera_widget: QGlPicamera2 | None = None
        self.exposure = 20.0
        self.preview_enabled = True
        self._drag_start = None

        self._recording = False
        self._run = True
        self._muxer_queue = queue.Queue()

        self._logger.info(f"Screen size: {QApplication.primaryScreen().size()}")

        screen_size = QApplication.primaryScreen().size()
        self.camera_widget_width = int(
            screen_size.width() / (1 - (poocam_config["cut_left"] + poocam_config["cut_right"])))
        self.camera_widget_height = screen_size.height()

        self._init_overlay()

        self._recording_encoder = H264Encoder(poocam_config["recording_bitrate"])
        self._pts_writer = None

        self.camera = Picamera2()
        camera_config = self.camera.create_video_configuration(
            main={"size": (poocam_config["video_width"], poocam_config["video_height"]), "format": "XBGR8888"},
            lores={"size": (poocam_config["low_res_width"], poocam_config["low_res_height"]), "format": "YUV420"},
            controls={"FrameRate": 30.62})
        self.camera.configure(camera_config)
        self.camera_widget = QGlPicamera2(self.camera, width=self.camera_widget_width,
                                          height=self.camera_widget_height, keep_ar=False)
        self.camera_scroll_widget.setWidget(self.camera_widget)
        self.camera.start()

        self.camera.set_controls({"AnalogueGain": self.exposure})

        # # metadata = self.camera.capture_metadata()
        # # controls = {c: metadata[c] for c in ["ExposureTime", "AnalogueGain", "ColourGains"]}
        # self._logger.info(f"Camera controls: {self.camera.controls}")

        self._motion_detector_thread = Thread(target=self.motion_detector)
        self._motion_detector_thread.start()

        self._muxer_thread = Thread(target=self.muxer)
        self._muxer_thread.start()

    # def _screen_to_overlay(self, screen_x, screen_y):
    #     self.camera_widget.size()
    #     overlay_width = poocam_config["video_width"]
    #     overlay_height = poocam_config["video_height"]

    def _init_overlay(self):
        overlay = QImage(self.camera_widget_width, self.camera_widget_height, QImage.Format_RGBA8888)
        overlay.fill(Qt.transparent)
        painter = QPainter(overlay)
        painter.setRenderHint(QPainter.Antialiasing, True)
        # painter.setPen(QColor(255, 0, 0, 127))
        painter.setBrush(QColor(255, 0, 0, 127))
        dot_size = 40
        painter.drawEllipse(overlay.width() // 2 + 250 - dot_size // 2, overlay.height() // 2 - 250 + dot_size // 2,
                            dot_size, dot_size)
        painter.end()
        ptr = overlay.bits()
        ptr.setsize(overlay.width() * overlay.height() * 4)
        self.overlay = np.copy(np.frombuffer(ptr, np.uint8).reshape((overlay.height(), overlay.width(), 4)))

    def start_recording(self):
        self._recording = True
        self.current_filename = datetime.datetime.now().strftime('%Y-%m-%d %H-%M-%S.%f')
        self._logger.info(f"Started recording {self.current_filename}")
        self._pts_writer = io.open(os.path.join(self.temp_directory, f"{self.current_filename}.txt"), "w")
        self._pts_writer.write("# timecode format v2\n")
        self._recording_encoder.output = FileOutput(os.path.join(self.temp_directory, f"{self.current_filename}.h264"),
                                                    pts=self._pts_writer)
        self.camera.start_encoder(encoder=self._recording_encoder)
        self.set_overlay(True)

    def stop_recording(self):
        if self._recording:
            self.camera.stop_encoder()
            if self._pts_writer:
                self._pts_writer.close()
            self.set_overlay(False)
            self._muxer_queue.put(self.current_filename)
            self._recording = False

    def set_overlay(self, enabled: bool):
        self.camera_widget.set_overlay(self.overlay if enabled else None)

    def resizeEvent(self, event):
        # self._logger.info(f"resize: {event.size()}")
        self.camera_scroll_widget.horizontalScrollBar().setValue(
            int(poocam_config["cut_left"] * self.camera_widget_width))
        QMainWindow.resizeEvent(self, event)

    # def mouseMoveEvent(self, event):
    #     self._logger.info(f"mouseMoveEvent {event.type()} {event.pos()}")
    #     event.accept()

    def wake(self):
        self.camera_widget.show()
        set_brightness(1)
        self.preview_enabled = True
        if self.screen_timeout > 0:
            self.sleep_timer.start(self.screen_timeout * 1000)

    def sleep(self):
        self.camera_widget.hide()
        set_brightness(0)
        self.preview_enabled = False

    def mousePressEvent(self, event):
        self._logger.debug(f"mousePressEvent: {event.type()}")
        if event.type() == QEvent.MouseButtonPress:
            if self.preview_enabled:
                self._drag_start = event.pos()
            else:
                self.wake()
        event.accept()

    def mouseReleaseEvent(self, event):
        self._logger.debug(f"mouseReleaseEvent: {event.type()}")
        if self._drag_start is not None:
            self._logger.debug(f"Drag from {self._drag_start} to {event.pos()}")
            drag_y = event.pos().y() - self._drag_start.y()
            if abs(drag_y) > 50:
                new_exposure = max(self.exposure - drag_y / 500, 0)
                self._logger.info(f"Changing exposure from {self.exposure} to {new_exposure}")
                self.exposure = new_exposure
                self.camera.set_controls({"AnalogueGain": self.exposure})
            else:
                self.sleep()
            self._drag_start = None
        event.accept()

        #     self.start_preview()
        # if event.pos().y() > self.camera_widget_height/2:
        # qDebug(f"mousePressEvent: {event.pos()}, exposure: {self.exposure}")
        # self.camera.set_controls({"AnalogueGain": self.exposure})

    def closeEvent(self, event):
        self._logger.info("Exiting")
        self.stop_recording()
        self._run = False
        set_brightness(1)
        time.sleep(1)
        event.accept()

    def motion_detector(self):
        previous = None
        last_activity = 0
        stabilized = False
        i = 0
        while self._run:
            current = self.camera.capture_buffer("lores")
            current = current[:poocam_config["low_res_width"] * poocam_config["low_res_height"]]. \
                reshape(poocam_config["low_res_height"], poocam_config["low_res_width"])  # luminance
            if previous is not None:
                mse = np.square(np.subtract(current, previous)).mean()
                if not stabilized and mse < self.mse_threshold:
                    stabilized = True
                if stabilized and mse > self.mse_threshold:
                    if not self._recording:
                        self.start_recording()
                    last_activity = time.monotonic()
                elif self._recording and time.monotonic() - last_activity > poocam_config["recording_timeout"]:
                    self.stop_recording()
                i += 1
                if i == 9:
                    self._logger.debug(f"MSE: {mse}")
                    i = 0
            previous = current
        self._logger.info("Exiting motion detector")

    def muxer(self):
        while self._run or not self._muxer_queue.empty():
            try:
                filename = self._muxer_queue.get(block=True, timeout=1)
            except queue.Empty:
                filename = None
            if filename:
                mkv_path = os.path.join(self.temp_directory, f"{filename}.mkv")
                pts_path = os.path.join(self.temp_directory, f"{filename}.txt")
                h264_path = os.path.join(self.temp_directory, f"{filename}.h264")
                result = subprocess.run(["mkvmerge", "-o", mkv_path, "--timecodes", f"0:{pts_path}", h264_path])
                if result.returncode != 0:
                    self._logger.error(f"Error while muxing {filename}")
                    continue
                self._logger.info(f"{filename} successfully muxed")
                try:
                    os.remove(h264_path)
                except OSError:
                    self._logger.error(f"Could not delete {h264_path}")
                try:
                    os.remove(pts_path)
                except OSError:
                    self._logger.error(f"Could not delete {pts_path}")
                try:
                    os.rename(mkv_path, os.path.join(self.target_directory, f"{filename}.mkv"))
                except OSError:
                    self._logger.error(f"Could not move {mkv_path}")
        self._logger.info("Exiting muxer")


@click.command()
@click.option("--temp-directory", type=click.Path(exists=True, file_okay=False), default="temp")
@click.option("--recordings-directory", type=click.Path(exists=True, file_okay=False), default="recordings")
@click.option("--mse-threshold", type=float, default=6)
@click.option("--screen-timeout", type=float, default=0)
@click.option("-v", '--verbose', count=True, help="Logging level")
@click.option("-l", "--log", is_flag=True, help="Enable logging to file")
def main(temp_directory: str, recordings_directory, mse_threshold, screen_timeout, verbose, log):
    log_handlers = [logging.StreamHandler()]
    log_level = {0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}.get(verbose, logging.WARNING)
    if log:
        log_handlers.append(logging.FileHandler(datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S-poocam.log")))
    logging.basicConfig(
        handlers=log_handlers,
        encoding='utf-8',
        level=log_level,
        format='%(asctime)s|%(levelname)s|%(name)s|%(message)s',
        datefmt='%Y-%m-%d %H:%M:%S')

    subprocess.run(["sudo", "chown", f"{os.getlogin()}:{os.getlogin()}", "/sys/waveshare/rpi_backlight/brightness"],
                   check=True)

    app = QApplication([])

    cursor = QCursor(Qt.BlankCursor)
    QApplication.setOverrideCursor(cursor)
    QApplication.changeOverrideCursor(cursor)

    window = PoocamMainWindow(temp_directory, recordings_directory, mse_threshold, screen_timeout)
    window.showFullScreen()

    signal.signal(signal.SIGINT, lambda _signum, _frame: window.close())

    app.exec()


if __name__ == "__main__":
    main()
