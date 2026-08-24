# Scan Rename

Automatically analyses scanned documents using OpenAI Vision (`gpt-4o`) and renames them with descriptive filenames. Processed files are tagged in macOS Finder so they are never touched again on future runs.

## How it works

1. Finds all files in the directory whose names start with `SCAN_` or `IMG_`
2. Skips any file already tagged `AI-Processed` (from a previous run)
3. Renders the first page of each PDF (or the image itself) and sends it to `gpt-4o`
4. Renames the file using the AI's suggested name (e.g. `NatWest Bank Statement Oct 2024.pdf`)
5. Applies two macOS Finder tags to the renamed file:
   - `AI-Processed` — prevents re-processing on future runs
   - A category tag (e.g. `Financial`, `Identity`, `Medical`) — visible in the Finder sidebar

Already-named files (those not starting with `SCAN_` or `IMG_`) are ignored entirely.

## Requirements

- macOS (uses native extended attributes for Finder tags)
- Python 3.9+
- An [OpenAI API key](https://platform.openai.com/account/api-keys) with access to `gpt-4o`

## Setup (first time only)

```bash
cd "<path to scanned docs>/Scanned Docs"
python3 -m venv .venv
.venv/bin/pip install openai PyMuPDF Pillow
```

> The `.venv` directory is already present if you received this folder with the environment pre-built.

### Store your OpenAI API key (once)

Store the key in macOS Keychain so the script can always find it without it ever appearing in shell history or config files:

```bash
printf 'Paste API key: ' && read -rs key && echo
security add-generic-password -a "$USER" -s "openai-api-key" -w "$key"
```

`read -rs` captures input silently (no echo, no shell interpretation), so hyphens and special characters in the key are preserved. The script reads the key from Keychain automatically on every run. If `OPENAI_API_KEY` is already set in your environment that takes precedence.

## Usage

```bash
bash run_scan_rename.sh
```

### Flags

| Flag | Description |
|---|---|
| `--dry-run` | Preview what would be renamed without making any changes |
| `--dir /path` | Process a different directory (default: same folder as the script) |

### Examples

```bash
# Preview changes
bash run_scan_rename.sh --dry-run

# Process a different folder
bash run_scan_rename.sh --dir ~/Desktop/more-scans
```

You can also call the Python script directly (with the venv active):

```bash
source .venv/bin/activate
python3 scan_rename.py --dry-run
```

## Auto-trigger on file drop (launchd)

On macOS you can have the script run automatically whenever a file appears in your scans folder using `launchd`.

**1. Create the launch agent plist** at `~/Library/LaunchAgents/com.user.scan-rename.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.scan-rename</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/path/to/scanned-docs-organiser/run_scan_rename.sh</string>
    </array>
    <key>WatchPaths</key>
    <array>
        <string>/path/to/your/scans/folder</string>
    </array>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>/Users/Shared/scan-rename.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/Shared/scan-rename.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
```

Replace both `/path/to/...` values with your actual paths. Keep the log path **outside** the watched folder to avoid triggering a second run when the log file is written. `ThrottleInterval` of 30 s absorbs the extra trigger that fires when the script renames a file inside the watched folder.

**2. Load the agent:**

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.scan-rename.plist
```

**3. Check it is running:**

```bash
launchctl print gui/$(id -u)/com.user.scan-rename
```

**Useful commands:**

```bash
# Trigger manually
launchctl kickstart -k gui/$(id -u)/com.user.scan-rename

# Watch the log live
tail -f /Users/Shared/scan-rename.log

# Stop and unload
launchctl bootout gui/$(id -u)/com.user.scan-rename
```

The agent loads automatically at login and persists across reboots.

## Category tags

The AI assigns one of the following categories to each document:

`Identity` · `Financial` · `Medical` · `Legal` · `Insurance` · `Utility` · `Receipt` · `Government` · `Employment` · `Education` · `Property` · `Correspondence` · `Other`

## File structure

```
Scanned Docs/
├── scan_rename.py       # Main Python script
├── run_scan_rename.sh   # Shell launcher (handles venv activation)
├── requirements.txt     # pip dependencies
├── .venv/               # Python virtual environment
└── README.md            # This file
```

## Notes

- If the API fails on a particular file (e.g. network error), the script logs the error and continues with the remaining files. Re-running will retry any that failed.
- If a suggested filename already exists, a number is appended automatically: `British Passport (2).pdf`.
- Only the **first page** is sent to the API. This is sufficient for identification and keeps API costs low.
- Approximate cost per document: ~$0.01–$0.02 using `gpt-4o` with high-detail vision.
