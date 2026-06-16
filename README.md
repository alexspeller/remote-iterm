# remote-iterm

Control your macOS iTerm2 from your phone over local network.

## Install

```bash
npm install -g remote-iterm
```

## Usage

```bash
remote-iterm          # start
remote-iterm stop     # stop
remote-iterm restart  # restart
```

Open the printed URL on your phone (same Wi-Fi network).

## Features

- Real-time terminal output with syntax coloring
- Tab management (create, close, rename via long-press)
- Split-pane switcher — view and control any pane in a split tab
- Multi-window support with spatial map
- Broadcast commands to multiple windows
- Command history with arrow navigation
- Virtual keyboard with terminal keys
- Quick action buttons (Ctrl+C, ESC, etc.)
- Clipboard paste/copy
- Landscape mode optimized for iPhone
- Dynamic Island / notch safe area handling
- Connection latency indicator
- Screen wake lock
- Long-running command alert
- Scroll lock
- PWA — add to home screen

## Requirements

- macOS with iTerm2
- iTerm2 **Python API enabled**: Settings → General → Magic → "Enable Python API"
- Python 3.8+ (Homebrew `python3` recommended; the launcher creates its own venv)
- Node.js >= 18 (serves the web UI)
- Phone on the same Wi-Fi

On first launch, iTerm2 asks macOS for a one-time Automation permission so the
server can connect to it — approve it once and it won't ask again.

## Manual Setup

```bash
git clone https://github.com/mammadovziya/remote-iterm.git
cd remote-iterm
cd client && npm install && cd ..
./iterm-server
```

The first `./iterm-server` run creates a Python virtualenv under `server/.venv`
and installs the server's dependencies automatically.

## Ports

- `7291` — WebSocket server
- `7292` — Vite dev server (UI)

## License

MIT
