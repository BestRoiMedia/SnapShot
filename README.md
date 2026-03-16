# SnapShot

A simple, lightweight area screenshot tool for Ubuntu/Linux.

![Python](https://img.shields.io/badge/Python-3.10+-blue) ![GTK](https://img.shields.io/badge/GTK-3.0-green) ![License](https://img.shields.io/badge/License-MIT-yellow)

## Features

- Click and drag to capture any area of your screen
- Configurable save folder
- Darkened overlay with bright selection highlight
- Dimension display while selecting
- Desktop notification on save
- Timestamped PNG filenames
- Lightweight — no heavy dependencies

## Requirements

- Python 3.10+
- GTK 3.0 (`python3-gi`)
- Pillow (`python3-pil`)

These are pre-installed on most Ubuntu systems.

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

1. Click **Take Screenshot**
2. Drag to select an area
3. Screenshot saves to your configured folder (default: `~/Pictures/Screenshots/`)
4. Press **Escape** to cancel

## Configuration

- Click **Change** to pick a different save folder
- Settings are stored in `~/.config/snapshot/config.json`

## License

MIT
