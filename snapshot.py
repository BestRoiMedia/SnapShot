#!/usr/bin/env python3
"""SnapShot - Simple area screenshot tool for Ubuntu.
Lives in the top panel as an indicator. Click to screenshot, right-click for settings.
No cairo bridge (python3-gi-cairo) needed — uses pixbuf compositing."""

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

# Support both Ayatana (modern Ubuntu) and legacy AppIndicator3
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageChops, ImageEnhance
from gi.repository import Gdk, GdkPixbuf, Gtk, GLib

CONFIG_DIR = Path.home() / ".config" / "snapshot"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_SAVE_DIR = str(Path.home() / "Pictures" / "Screenshots")


def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"save_dir": DEFAULT_SAVE_DIR}


def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def darken_pixbuf(pixbuf, factor=0.55):
    """Return a darkened copy using PIL for speed (~80ms at 4K)."""
    w = pixbuf.get_width()
    h = pixbuf.get_height()
    nc = pixbuf.get_n_channels()
    rs = pixbuf.get_rowstride()
    pixels = pixbuf.get_pixels()
    mode = "RGBA" if nc == 4 else "RGB"
    img = Image.frombuffer(mode, (w, h), pixels, "raw", mode, rs, 1)
    dark_img = ImageEnhance.Brightness(img).enhance(factor)
    # Keep a reference to the bytes so GdkPixbuf doesn't read freed memory
    dark_bytes = dark_img.tobytes("raw", mode)
    pb = GdkPixbuf.Pixbuf.new_from_data(
        dark_bytes, pixbuf.get_colorspace(), nc == 4,
        pixbuf.get_bits_per_sample(), w, h, w * nc,
    )
    pb._dark_bytes = dark_bytes  # prevent GC
    return pb


def _pixbuf_to_pil(pixbuf):
    """Convert a GdkPixbuf to a PIL Image."""
    w = pixbuf.get_width()
    h = pixbuf.get_height()
    nc = pixbuf.get_n_channels()
    rs = pixbuf.get_rowstride()
    pixels = pixbuf.get_pixels()
    mode = "RGBA" if nc == 4 else "RGB"
    return Image.frombuffer(mode, (w, h), pixels, "raw", mode, rs, 1)


def _show_error(message):
    """Show a GTK error dialog."""
    dialog = Gtk.MessageDialog(
        parent=None,
        flags=0,
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.OK,
        text=message,
    )
    dialog.run()
    dialog.destroy()


def _mean_diff(img1, img2):
    """Mean per-pixel difference between two same-sized images (0-255 scale)."""
    diff = ImageChops.difference(img1.convert("L"), img2.convert("L"))
    hist = diff.histogram()
    total = sum(i * count for i, count in enumerate(hist))
    return total / (img1.size[0] * img1.size[1])


def _images_similar(img1, img2, threshold=3):
    """Check if two same-sized images are nearly identical."""
    return _mean_diff(img1, img2) < threshold


class AreaSelector(Gtk.Window):
    """Fullscreen overlay for drag-selecting a screen region.
    Uses Gtk.Image layers to avoid needing python3-gi-cairo."""

    def __init__(self, callback, screenshot, mode="pixbuf"):
        super().__init__(title="Select Area")
        self.callback = callback
        self.screenshot = screenshot
        self.mode = mode  # "pixbuf" returns cropped pixbuf, "coords" returns (x, y, w, h)
        self.start_x = 0
        self.start_y = 0
        self.end_x = 0
        self.end_y = 0
        self.dragging = False

        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_keep_above(True)
        self.fullscreen()

        # Build the darkened background
        self.dark_pb = darken_pixbuf(screenshot)

        # Use Gtk.Overlay: dark bg image on bottom, bright selection on top
        overlay = Gtk.Overlay()
        self.bg_image = Gtk.Image.new_from_pixbuf(self.dark_pb)
        overlay.add(self.bg_image)

        # Fixed container for the bright selection crop
        self.fixed = Gtk.Fixed()
        self.sel_image = Gtk.Image()
        self.fixed.put(self.sel_image, 0, 0)

        # Border frame around selection (using an EventBox with CSS)
        self.sel_frame = Gtk.EventBox()
        self.sel_frame.set_visible(False)
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            .sel-border {
                border: 2px solid #3399ff;
                background: transparent;
            }
        """)
        self.sel_frame.get_style_context().add_provider(
            css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.sel_frame.get_style_context().add_class("sel-border")
        self.fixed.put(self.sel_frame, 0, 0)

        # Size label
        self.size_label = Gtk.Label()
        self.size_label.set_visible(False)
        lbl_css = Gtk.CssProvider()
        lbl_css.load_from_data(b"""
            .size-lbl {
                background: rgba(0,0,0,0.75);
                color: white;
                padding: 2px 6px;
                border-radius: 3px;
                font-size: 12px;
            }
        """)
        self.size_label.get_style_context().add_provider(
            lbl_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.size_label.get_style_context().add_class("size-lbl")
        self.fixed.put(self.size_label, 0, 0)

        overlay.add_overlay(self.fixed)
        overlay.set_overlay_pass_through(self.fixed, True)

        # Transparent event catcher on top
        self.event_box = Gtk.EventBox()
        self.event_box.set_visible_window(False)
        self.event_box.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.event_box.connect("button-press-event", self.on_button_press)
        self.event_box.connect("button-release-event", self.on_button_release)
        self.event_box.connect("motion-notify-event", self.on_motion)
        overlay.add_overlay(self.event_box)

        self.add(overlay)
        self.connect("key-press-event", self.on_key_press)

        def on_realize(widget):
            cursor = Gdk.Cursor.new_from_name(widget.get_display(), "crosshair")
            widget.get_window().set_cursor(cursor)

        self.connect("realize", on_realize)

    def _update_selection(self):
        x = min(self.start_x, self.end_x)
        y = min(self.start_y, self.end_y)
        w = abs(self.end_x - self.start_x)
        h = abs(self.end_y - self.start_y)

        if w < 2 or h < 2:
            self.sel_image.set_visible(False)
            self.sel_frame.set_visible(False)
            self.size_label.set_visible(False)
            return

        # Clamp to pixbuf bounds
        pb_w = self.screenshot.get_width()
        pb_h = self.screenshot.get_height()
        x = max(0, min(x, pb_w - 1))
        y = max(0, min(y, pb_h - 1))
        w = min(w, pb_w - x)
        h = min(h, pb_h - y)

        if w < 2 or h < 2:
            return

        # Show bright (original) crop at the selection position
        crop = self.screenshot.new_subpixbuf(x, y, w, h)
        self.sel_image.set_from_pixbuf(crop)
        self.sel_image.set_visible(True)
        self.fixed.move(self.sel_image, x, y)

        # Position border frame
        self.sel_frame.set_size_request(w, h)
        self.sel_frame.set_visible(True)
        self.fixed.move(self.sel_frame, x, y)

        # Size label
        self.size_label.set_text(f"  {w} x {h}  ")
        self.size_label.set_visible(True)
        lbl_y = y - 25 if y > 30 else y + h + 5
        self.fixed.move(self.size_label, x, lbl_y)

    def on_button_press(self, widget, event):
        if event.button == 1:
            self.start_x = int(event.x)
            self.start_y = int(event.y)
            self.end_x = self.start_x
            self.end_y = self.start_y
            self.dragging = True

    def on_button_release(self, widget, event):
        if event.button == 1 and self.dragging:
            self.dragging = False
            self.end_x = int(event.x)
            self.end_y = int(event.y)

            x = min(self.start_x, self.end_x)
            y = min(self.start_y, self.end_y)
            w = abs(self.end_x - self.start_x)
            h = abs(self.end_y - self.start_y)

            if w > 5 and h > 5:
                pb_w = self.screenshot.get_width()
                pb_h = self.screenshot.get_height()
                x = max(0, min(x, pb_w - 1))
                y = max(0, min(y, pb_h - 1))
                w = min(w, pb_w - x)
                h = min(h, pb_h - y)
                self.hide()
                if self.mode == "coords":
                    self.callback((x, y, w, h))
                else:
                    pixbuf = self.screenshot.new_subpixbuf(x, y, w, h)
                    self.callback(pixbuf)
                self.destroy()
            else:
                self.hide()
                self.callback(None)
                self.destroy()

    def on_motion(self, widget, event):
        if self.dragging:
            self.end_x = int(event.x)
            self.end_y = int(event.y)
            self._update_selection()

    def on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.dragging = False
            self.callback(None)
            self.destroy()


class RecordingControls(Gtk.Window):
    """Floating 'REC 0:00 [Stop]' control bar shown during GIF recording."""

    def __init__(self, stop_callback, label_prefix="REC"):
        super().__init__(title="Recording")
        self.stop_callback = stop_callback
        self.label_prefix = label_prefix
        self.elapsed = 0

        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_default_size(180, 40)
        self.set_resizable(False)

        # Position at top-center of screen
        screen = Gdk.Screen.get_default()
        self.move(screen.get_width() // 2 - 90, 10)

        css = Gtk.CssProvider()
        css.load_from_data(b"""
            .rec-bar {
                background: #cc0000;
                border-radius: 8px;
                padding: 4px 12px;
            }
            .rec-label {
                color: white;
                font-weight: bold;
                font-size: 14px;
            }
            .rec-stop {
                background: white;
                color: #cc0000;
                font-weight: bold;
                border-radius: 4px;
                padding: 2px 12px;
                border: none;
            }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            screen, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        hbox.get_style_context().add_class("rec-bar")

        self.label = Gtk.Label(label=f"{self.label_prefix}  0:00")
        self.label.get_style_context().add_class("rec-label")
        hbox.pack_start(self.label, True, True, 0)

        stop_btn = Gtk.Button(label="Stop")
        stop_btn.get_style_context().add_class("rec-stop")
        stop_btn.connect("clicked", self._on_stop)
        hbox.pack_end(stop_btn, False, False, 0)

        self.add(hbox)
        self._timer_id = GLib.timeout_add(1000, self._tick)

    def _tick(self):
        self.elapsed += 1
        mins = self.elapsed // 60
        secs = self.elapsed % 60
        self.label.set_text(f"{self.label_prefix}  {mins}:{secs:02d}")
        return True

    def _on_stop(self, widget):
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = None
        self.hide()
        self.stop_callback()
        self.destroy()


class SnapShotIndicator:
    """System tray indicator — left-click to screenshot, right-click for menu."""

    def __init__(self):
        self.config = load_config()
        Path(self.config["save_dir"]).mkdir(parents=True, exist_ok=True)

        # Create the indicator in the top panel
        self.indicator = AppIndicator3.Indicator.new(
            "snapshot-indicator",
            "applets-screenshooter",
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("SnapShot")

        # Build the right-click menu
        self.menu = Gtk.Menu()

        item_screenshot = Gtk.MenuItem(label="Take Screenshot")
        item_screenshot.connect("activate", self.on_take_screenshot)
        self.menu.append(item_screenshot)

        item_scrolling = Gtk.MenuItem(label="Scrolling Capture")
        item_scrolling.connect("activate", self.on_scrolling_capture)
        self.menu.append(item_scrolling)

        item_gif = Gtk.MenuItem(label="Record GIF")
        item_gif.connect("activate", self.on_record_gif)
        self.menu.append(item_gif)

        self.menu.append(Gtk.SeparatorMenuItem())

        self.folder_item = Gtk.MenuItem(
            label=f"Save to: {self._short_path(self.config['save_dir'])}"
        )
        self.folder_item.set_sensitive(False)
        self.menu.append(self.folder_item)

        item_change_folder = Gtk.MenuItem(label="Change Save Folder...")
        item_change_folder.connect("activate", self.on_change_folder)
        self.menu.append(item_change_folder)

        item_open_folder = Gtk.MenuItem(label="Open Screenshots Folder")
        item_open_folder.connect("activate", self.on_open_folder)
        self.menu.append(item_open_folder)

        self.menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", self.on_quit)
        self.menu.append(item_quit)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)

        # Left-click: use the secondary activate signal to take a screenshot
        self.indicator.connect("scroll-event", lambda *a: None)
        self.indicator.set_secondary_activate_target(item_screenshot)

    def _short_path(self, path):
        home = str(Path.home())
        if path.startswith(home):
            return "~" + path[len(home):]
        return path

    def on_take_screenshot(self, widget):
        # Small delay to let any menu close before capturing
        GLib.timeout_add(350, self._start_selection)

    def _start_selection(self):
        root = Gdk.get_default_root_window()
        w = root.get_width()
        h = root.get_height()
        screenshot = Gdk.pixbuf_get_from_window(root, 0, 0, w, h)
        selector = AreaSelector(self._on_screenshot_done, screenshot)
        selector.show_all()
        return False

    def _on_screenshot_done(self, pixbuf):
        if pixbuf is None:
            return

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"screenshot_{timestamp}.png"
        filepath = os.path.join(self.config["save_dir"], filename)

        Path(self.config["save_dir"]).mkdir(parents=True, exist_ok=True)
        pixbuf.savev(filepath, "png", [], [])

        self._notify(f"Saved: {filename}", filepath)

    def _notify(self, message, filepath):
        try:
            subprocess.Popen(
                ["notify-send", "SnapShot", message, "-i", filepath, "-t", "3000"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass

    # --- Scrolling Capture ---

    def on_scrolling_capture(self, widget):
        GLib.timeout_add(350, self._start_scrolling_selection)

    def _start_scrolling_selection(self):
        root = Gdk.get_default_root_window()
        w = root.get_width()
        h = root.get_height()
        screenshot = Gdk.pixbuf_get_from_window(root, 0, 0, w, h)
        selector = AreaSelector(self._on_scrolling_area_selected, screenshot,
                                mode="coords")
        selector.show_all()
        return False

    def _on_scrolling_area_selected(self, coords):
        if coords is None:
            return
        self._scroll_coords = coords
        self._scroll_frames = []
        self._scroll_prev_frame = None
        # Small delay to let the overlay disappear
        GLib.timeout_add(300, self._start_scrolling_recording)

    def _start_scrolling_recording(self):
        # Capture the initial frame
        self._capture_scroll_frame()
        # Start periodic capture every 300ms
        self._scroll_timer_id = GLib.timeout_add(300, self._capture_scroll_frame)
        # Show control bar
        self._scroll_controls = RecordingControls(
            self._stop_scrolling_capture, label_prefix="SCROLL"
        )
        self._scroll_controls.show_all()
        return False

    def _capture_scroll_frame(self):
        x, y, w, h = self._scroll_coords
        root = Gdk.get_default_root_window()
        pixbuf = Gdk.pixbuf_get_from_window(root, x, y, w, h)
        if pixbuf is None:
            return True

        frame = _pixbuf_to_pil(pixbuf)

        # Skip if nearly identical to the last captured frame (no scrolling)
        if self._scroll_prev_frame is not None:
            if _images_similar(frame, self._scroll_prev_frame):
                return True

        self._scroll_prev_frame = frame.copy()
        self._scroll_frames.append(frame)
        return True

    def _stop_scrolling_capture(self):
        if hasattr(self, "_scroll_timer_id") and self._scroll_timer_id:
            GLib.source_remove(self._scroll_timer_id)
            self._scroll_timer_id = None
        GLib.idle_add(self._stitch_scroll_frames)

    def _find_scroll_amount(self, prev, curr):
        """Find how many pixels were scrolled between two frames.

        When the page scrolls down by d pixels, content at position y in prev
        appears at position y-d in curr.

        Uses the entire bottom half of prev as a reference (downsampled 4x
        for speed).  A large reference region is far more robust against
        repeating page layouts (card grids, etc.) than a thin band.

        Validates the best match with a second reference from the top half.

        Returns d (> 0) on success, -1 if no reliable match.
        """
        h = prev.height
        w = prev.width
        if h < 16 or w < 16:
            return -1

        scale = 4
        sw, sh = w // scale, h // scale
        if sw < 4 or sh < 4:
            return -1

        sp = prev.convert("L").resize((sw, sh), Image.NEAREST)
        sc = curr.convert("L").resize((sw, sh), Image.NEAREST)

        # --- Primary: bottom-half reference ---
        ref_top = sh // 2
        ref = sp.crop((0, ref_top, sw, sh))
        ref_h = sh - ref_top

        best_d = 0
        best_diff = 999.0
        for ds in range(1, ref_top + 1):
            sy = ref_top - ds
            if sy + ref_h > sh:
                continue
            test = sc.crop((0, sy, sw, sy + ref_h))
            diff = _mean_diff(ref, test)
            if diff < best_diff:
                best_diff = diff
                best_d = ds
            if diff < 0.8:
                break

        if best_diff > 6:
            return -1

        # --- Validate: top-quarter reference ---
        # A band from 15-25% of prev should shift by the same amount.
        val_y = int(sh * 0.15)
        val_h = int(sh * 0.10) or 1
        if val_y + val_h <= sh and val_y - best_d >= 0:
            val_ref = sp.crop((0, val_y, sw, val_y + val_h))
            val_test = sc.crop((0, val_y - best_d, sw, val_y - best_d + val_h))
            vdiff = _mean_diff(val_ref, val_test)
            if vdiff > 12:
                return -1

        # --- Refine at full resolution ---
        # The coarse result is accurate to ±scale pixels.  Try multiple
        # reference bands (at 35%, 50%, 65% height) and pick the offset
        # that gets the best overall agreement.
        coarse_d = best_d * scale
        band_h = min(24, h // 6)
        prev_l = prev.convert("L")
        curr_l = curr.convert("L")
        search_lo = max(1, coarse_d - scale - 1)
        search_hi = min(coarse_d + scale + 2, h)

        # Accumulate diff scores for each candidate offset
        scores = {}
        for d in range(search_lo, search_hi):
            scores[d] = 0.0

        for ref_pct in (0.35, 0.50, 0.65):
            ref_y = int(h * ref_pct)
            if ref_y + band_h > h:
                continue
            ref_band = prev_l.crop((0, ref_y, w, ref_y + band_h))
            for d in range(search_lo, search_hi):
                sy = ref_y - d
                if sy < 0 or sy + band_h > h:
                    scores[d] += 50.0  # penalty for out-of-bounds
                    continue
                test = curr_l.crop((0, sy, w, sy + band_h))
                scores[d] += _mean_diff(ref_band, test)

        fine_d = min(scores, key=scores.get)

        # Add 1px overlap bias — a tiny overlap is invisible, but a 1px
        # gap or duplicated row creates a visible seam.
        if fine_d + 1 in scores and scores[fine_d + 1] - scores[fine_d] < 0.5:
            fine_d += 1

        return fine_d

    def _stitch_scroll_frames(self):
        frames = self._scroll_frames
        if not frames:
            return False

        x, y, w, h = self._scroll_coords

        strips = [frames[0]]
        last_matched = frames[0]  # last frame we successfully matched

        for i in range(1, len(frames)):
            curr = frames[i]

            # Skip nearly identical frames
            if _images_similar(last_matched, curr, threshold=2):
                continue

            scroll_d = self._find_scroll_amount(last_matched, curr)

            if scroll_d > 0:
                # Append only the newly revealed content at the bottom
                new_part = curr.crop((0, curr.height - scroll_d,
                                      curr.width, curr.height))
                if new_part.height > 0:
                    strips.append(new_part)
                last_matched = curr
            # scroll_d == -1: no reliable match — skip this frame.
            # Keep last_matched unchanged so the next frame compares
            # against the last good reference.

        # Stitch all strips vertically
        total_height = sum(s.height for s in strips)
        result = Image.new(strips[0].mode, (w, total_height))
        y_offset = 0
        for s in strips:
            result.paste(s, (0, y_offset))
            y_offset += s.height

        # Save
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"scrolling_{timestamp}.png"
        filepath = os.path.join(self.config["save_dir"], filename)
        Path(self.config["save_dir"]).mkdir(parents=True, exist_ok=True)
        result.save(filepath, "PNG")

        self._notify(f"Saved: {filename}", filepath)

        # Cleanup
        self._scroll_frames = []
        self._scroll_prev_frame = None
        return False

    # --- GIF Recording ---

    def on_record_gif(self, widget):
        if not shutil.which("ffmpeg"):
            GLib.idle_add(_show_error,
                          "GIF Recording requires ffmpeg.\n\n"
                          "Install it with:\n  sudo apt install ffmpeg")
            return
        GLib.timeout_add(350, self._start_gif_selection)

    def _start_gif_selection(self):
        root = Gdk.get_default_root_window()
        w = root.get_width()
        h = root.get_height()
        screenshot = Gdk.pixbuf_get_from_window(root, 0, 0, w, h)
        selector = AreaSelector(self._on_gif_area_selected, screenshot,
                                mode="coords")
        selector.show_all()
        return False

    def _on_gif_area_selected(self, coords):
        if coords is None:
            return
        x, y, w, h = coords
        # Small delay to let the overlay disappear
        GLib.timeout_add(300, self._start_gif_recording, x, y, w, h)

    def _start_gif_recording(self, x, y, w, h):
        self._gif_tmpdir = tempfile.mkdtemp(prefix="snapshot_gif_")
        self._gif_raw = os.path.join(self._gif_tmpdir, "recording.mkv")
        display = os.environ.get("DISPLAY", ":0")

        # Ensure even dimensions (required by ffmpeg)
        w = w if w % 2 == 0 else w - 1
        h = h if h % 2 == 0 else h - 1

        self._ffmpeg_proc = subprocess.Popen(
            [
                "ffmpeg", "-y",
                "-f", "x11grab",
                "-framerate", "15",
                "-video_size", f"{w}x{h}",
                "-i", f"{display}+{x},{y}",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p",
                self._gif_raw,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        self._rec_controls = RecordingControls(self._stop_gif_recording)
        self._rec_controls.show_all()
        return False

    def _stop_gif_recording(self):
        if hasattr(self, "_ffmpeg_proc") and self._ffmpeg_proc.poll() is None:
            # Send 'q' to ffmpeg to stop gracefully
            try:
                self._ffmpeg_proc.stdin.write(b"q")
                self._ffmpeg_proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._ffmpeg_proc.terminate()
            self._ffmpeg_proc.wait(timeout=10)

        # Convert to GIF using 2-pass palettegen for quality
        GLib.idle_add(self._convert_to_gif)

    def _convert_to_gif(self):
        palette = os.path.join(self._gif_tmpdir, "palette.png")
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"recording_{timestamp}.gif"
        filepath = os.path.join(self.config["save_dir"], filename)
        Path(self.config["save_dir"]).mkdir(parents=True, exist_ok=True)

        # Pass 1: generate palette
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", self._gif_raw,
                "-vf", "fps=15,palettegen=stats_mode=diff",
                palette,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        # Pass 2: create GIF with palette
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", self._gif_raw, "-i", palette,
                "-lavfi", "fps=15[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5",
                filepath,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        # Clean up temp files
        shutil.rmtree(self._gif_tmpdir, ignore_errors=True)

        if os.path.exists(filepath):
            self._notify(f"Saved: {filename}", filepath)

        return False

    def on_change_folder(self, widget):
        dialog = Gtk.FileChooserDialog(
            title="Choose Screenshots Folder",
            parent=None,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        dialog.set_current_folder(self.config["save_dir"])

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            new_dir = dialog.get_filename()
            self.config["save_dir"] = new_dir
            save_config(self.config)
            self.folder_item.set_label(
                f"Save to: {self._short_path(new_dir)}"
            )

        dialog.destroy()

    def on_open_folder(self, widget):
        subprocess.Popen(
            ["xdg-open", self.config["save_dir"]],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def on_quit(self, widget):
        Gtk.main_quit()


def main():
    indicator = SnapShotIndicator()
    Gtk.main()


if __name__ == "__main__":
    main()
