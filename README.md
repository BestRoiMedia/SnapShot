# SnapShot

A simple, lightweight area screenshot tool for Ubuntu/Linux that lives in your top panel.

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![GTK](https://img.shields.io/badge/GTK-3.0-green) ![License](https://img.shields.io/badge/License-MIT-yellow)

## Features

- **Top panel indicator** — always available next to your clock
- **Left-click** the icon to instantly take an area screenshot
- **Right-click** for settings: change save folder, open screenshots folder, or quit
- Click and drag to capture any area of your screen
- Darkened overlay with bright selection highlight
- Dimension display while selecting
- **Scrolling Capture** — select an area, scroll manually at your own pace, click Stop to stitch into one tall image
- **Record GIF** — select an area, record video, convert to animated GIF with high-quality palette
- Desktop notification on save
- Timestamped PNG/GIF filenames
- Lightweight — no window needed

## Requirements

- Python 3.10+
- GTK 3.0 (`python3-gi`)
- AppIndicator3 (`gir1.2-ayatanaappindicator3-0.1`)
- Pillow (`python3-pil`)

### Optional (for Record GIF)

- `ffmpeg` — required for Record GIF

Install all dependencies:

```bash
sudo apt install gir1.2-ayatanaappindicator3-0.1 ffmpeg
```

## Install

```bash
git clone https://github.com/BestRoiMedia/SnapShot.git
cd SnapShot
chmod +x snapshot.py
```

### Desktop Launcher

Copy the `.desktop` file to your applications:

```bash
cp snapshot.desktop ~/.local/share/applications/
```

Edit `snapshot.desktop` and update the `Exec=` path to point to your `snapshot.py` location.

## Usage

```bash
python3 snapshot.py
```

A camera icon appears in your top panel:

1. **Left-click** the icon to take a screenshot
2. Drag to select an area
3. Screenshot saves to your configured folder (default: `~/Pictures/Screenshots/`)
4. Press **Escape** to cancel

### Scrolling Capture

1. Right-click the indicator and select **Scrolling Capture**
2. Drag to select the area you want to capture (e.g., the content area of a browser)
3. A red **SCROLL** bar appears at the top of the screen — scroll through the content at your own pace
4. Click **Stop** when you've scrolled to the end
5. SnapShot stitches the captured frames into one tall PNG, automatically removing overlap
6. Saved as `scrolling_YYYY-MM-DD_HH-MM-SS.png`

### Record GIF

1. Right-click the indicator and select **Record GIF**
2. Drag to select the area to record
3. A red **REC** bar appears at the top of the screen with a timer
4. Click **Stop** when done
5. The recording is converted to an optimized GIF (2-pass palette for quality)
6. Saved as `recording_YYYY-MM-DD_HH-MM-SS.gif`

### Settings (right-click menu)

- **Change Save Folder** — pick a different save location
- **Open Screenshots Folder** — view saved screenshots
- **Quit** — remove the indicator

Settings are stored in `~/.config/snapshot/config.json`

## License

MIT
