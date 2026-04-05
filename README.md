<div align="center">

<img src="icon.png" alt="Secret Browser" width="120" height="120" />

# Secret Browser

### A desktop browser that doesn’t need to shout.  
*Python · PyQt6 · Chromium (Qt WebEngine)*

[![Python](https://img.shields.io/badge/python-3.10+-111111?style=for-the-badge&logo=python&logoColor=8ab4f8)](https://www.python.org/)
[![PyQt6](https://img.shields.io/badge/PyQt6-WebEngine-1a1a1d?style=for-the-badge&logo=qt&logoColor=a78bfa)](https://www.riverbankcomputing.com/software/pyqt/)
[![Platform](https://img.shields.io/badge/platform-Windows-141416?style=for-the-badge&logo=windows&logoColor=9aa0a8)](https://github.com/Dr1nnas/python-browser)

[**Features**](#-features) · [**Install**](#-install) · [**Shortcuts**](#-keyboard-shortcuts) · [**Privacy**](#-privacy)

</div>

---

<br/>

Secret Browser is a **frameless, dark‑themed** web browser built in Python.  
The UI is custom (tabs, title bar, chrome); the renderer is **Qt WebEngine**—the same Chromium stack that powers many embedded browsers—tuned to feel closer to desktop Chrome (UA, Client Hints, HTTPS‑first navigation).

> *Search or type a URL in one bar. Save pages. Clear data when you want—or leave no local trace on exit. Your call.*

<br/>

## Features

| | |
|:---|:---|
| **Omnibox** | Search with your chosen engine or open sites directly; smart handling of `file://` / blob URLs after downloads. |
| **Privacy knobs** | Optional **DNS-over-HTTPS** (Chromium flag), **HTTPS-first** for public sites, **third‑party cookie filter**, and **clear browsing data** on demand. |
| **“No trace” mode** | Optional wipe of cookies, cache, site storage—and bookmarks—**on quit** (configurable at first run or in Options). |
| **Bookmarks** | Star menu: save with **URL as default name**, rename anytime; open from the dropdown. |
| **Downloads** | Built-in download flow with a **Downloads** panel and history. |
| **First-run setup** | Pick search engine, privacy profile, and cookie blocking—no clutter later. |
| **Look & feel** | Custom min / max / close, edge resize grips, SVG toolbar icons, cohesive dark palette. |

---

## Install

**Requirements:** Python 3.10+ (recommended), 64‑bit Windows.

```bash
git clone https://github.com/Dr1nnas/python-browser.git
cd python-browser
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python python_browser.py
```

Dependencies are pinned in `requirements.txt` (PyQt6 + PyQt6-WebEngine aligned).

---

## Keyboard shortcuts

| Shortcut | Action |
|:--|:--|
| `Ctrl+T` | New tab |
| `Ctrl+W` | Close tab (last tab resets to home) |
| `Ctrl+R` | Reload |
| `Ctrl+L` | Focus address bar |
| `Ctrl+D` | Bookmark this page (name defaults to URL; editable) |
| `Ctrl+J` | Downloads |
| `Alt+←` / `Alt+→` | Back / Forward |
| `Alt+Home` | Home / new tab page |

---

## Privacy

- **HTTPS-first** for typical web hosts; local / private LAN HTTP is left usable for dev and routers.  
- **DNS-over-HTTPS** is enabled in automatic mode when Chromium supports it (see environment / Qt WebEngine docs).  
- **Third-party cookies** can be blocked via the cookie store filter (on by default in setup; change in **Menu → Options**).  
- **Clear browsing data** clears cache, cookies, visited-link hints, and on-disk storage paths for the profile.  
- **“No trace” on exit** removes local profile data and saved bookmarks from settings when enabled—**not** a substitute for Tor or a VPN; traffic still goes from your machine to sites as usual.

---

## Repository layout

```
python-browser/
├── python_browser.py      # App entry + UI
├── requirements.txt
├── assets/
│   ├── home/              # New tab + first-run HTML/CSS
│   └── icons/             # SVG toolbar icons
├── icon.png
└── README.md
```

---

## Pushing updates (maintainers)

If you use the included scripts: put a GitHub PAT in **`github-token.txt`** (gitignored), then run **`upload to github.bat`** (optional commit message as first argument). See `upload-to-github.ps1` for behavior.

---

## Caveats

Some sites (games, banks, strict SSO) **expect stock Chrome** and may block or break embedded engines. If something critical fails, use a mainstream browser for that flow.

---

<div align="center">

**Secret Browser** — *quiet chrome, loud privacy options.*

<sub>Repo: <a href="https://github.com/Dr1nnas/python-browser">github.com/Dr1nnas/python-browser</a></sub>

</div>
