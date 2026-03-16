#!/usr/bin/env python3
"""SnapShot - Simple area screenshot tool for Ubuntu.
No cairo bridge (python3-gi-cairo) needed — uses pixbuf compositing."""

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageEnhance
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


class AreaSelector(Gtk.Window):
    """Fullscreen overlay for drag-selecting a screen region.
    Uses Gtk.Image layers to avoid needing python3-gi-cairo."""

    def __init__(self, callback, screenshot):
        super().__init__(title="Select Area")
        self.callback = callback
        self.screenshot = screenshot
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
                pixbuf = self.screenshot.new_subpixbuf(x, y, w, h)
                self.hide()
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


class SnapShotApp(Gtk.Window):
    """Main application window."""

    def __init__(self):
        super().__init__(title="SnapShot")
        self.config = load_config()
        self.set_default_size(320, 180)
        self.set_resizable(False)
        self.set_position(Gtk.WindowPosition.CENTER)

        Path(self.config["save_dir"]).mkdir(parents=True, exist_ok=True)

        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title("SnapShot")
        header.set_subtitle(self._short_path(self.config["save_dir"]))
        self.header = header
        self.set_titlebar(header)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        vbox.set_margin_top(24)
        vbox.set_margin_bottom(24)
        vbox.set_margin_start(24)
        vbox.set_margin_end(24)

        btn_screenshot = Gtk.Button(label="Take Screenshot")
        btn_screenshot.get_style_context().add_class("suggested-action")
        btn_screenshot.set_size_request(-1, 48)
        btn_screenshot.connect("clicked", self.on_take_screenshot)
        vbox.pack_start(btn_screenshot, False, False, 0)

        folder_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        folder_label = Gtk.Label(label="Save to:")
        folder_label.set_xalign(0)
        self.folder_display = Gtk.Label(label=self._short_path(self.config["save_dir"]))
        self.folder_display.set_xalign(0)
        self.folder_display.set_ellipsize(3)
        btn_folder = Gtk.Button(label="Change")
        btn_folder.connect("clicked", self.on_change_folder)

        folder_box.pack_start(folder_label, False, False, 0)
        folder_box.pack_start(self.folder_display, True, True, 0)
        folder_box.pack_end(btn_folder, False, False, 0)
        vbox.pack_start(folder_box, False, False, 0)

        btn_open = Gtk.Button(label="Open Screenshots Folder")
        btn_open.connect("clicked", self.on_open_folder)
        vbox.pack_start(btn_open, False, False, 0)

        self.add(vbox)
        self.connect("destroy", Gtk.main_quit)

    def _short_path(self, path):
        home = str(Path.home())
        if path.startswith(home):
            return "~" + path[len(home):]
        return path

    def on_take_screenshot(self, button):
        self.hide()
        # Let the window fully disappear, then capture
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
            self.show_all()
            return

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"screenshot_{timestamp}.png"
        filepath = os.path.join(self.config["save_dir"], filename)

        Path(self.config["save_dir"]).mkdir(parents=True, exist_ok=True)
        pixbuf.savev(filepath, "png", [], [])

        self._notify(f"Saved: {filename}", filepath)
        self.show_all()

    def _notify(self, message, filepath):
        try:
            subprocess.Popen(
                ["notify-send", "SnapShot", message, "-i", filepath, "-t", "3000"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass

        dialog = Gtk.MessageDialog(
            transient_for=self,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="Screenshot Saved",
        )
        dialog.format_secondary_text(filepath)
        dialog.run()
        dialog.destroy()

    def on_change_folder(self, button):
        dialog = Gtk.FileChooserDialog(
            title="Choose Screenshots Folder",
            parent=self,
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
            self.folder_display.set_text(self._short_path(new_dir))
            self.header.set_subtitle(self._short_path(new_dir))

        dialog.destroy()

    def on_open_folder(self, button):
        subprocess.Popen(
            ["xdg-open", self.config["save_dir"]],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def main():
    app = SnapShotApp()
    app.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
