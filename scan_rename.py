#!/usr/bin/env python3
"""
scan_rename.py
==============
Analyses unprocessed scans using OpenAI Vision (gpt-4o) and renames/tags them.

Candidates are files whose names start with SCAN_ or IMG_.
After a file is processed it receives two macOS Finder tags:
  • "AI-Processed"  — prevents it being picked up on future runs
  • <category>      — e.g. "Identity", "Financial", "Medical" …

Usage
-----
    python3 scan_rename.py                  # process current directory
    python3 scan_rename.py --dry-run        # preview without renaming
    python3 scan_rename.py --dir /some/path # specify a different directory

Requirements
------------
    pip install openai PyMuPDF Pillow

Environment
-----------
    OPENAI_API_KEY  — your OpenAI API key (required)
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import io
import json
import logging
import os
import plistlib
import re
import sys
from pathlib import Path

# ── Optional-import guard ──────────────────────────────────────────────────────

def _import_or_exit(package: str, attr: str | None = None):
    import importlib
    try:
        mod = importlib.import_module(package)
        return getattr(mod, attr) if attr else mod
    except ImportError:
        print(
            f"\nMissing package '{package}'.\n"
            f"Install all requirements with:\n\n"
            f"    pip install openai PyMuPDF Pillow\n",
            file=sys.stderr,
        )
        sys.exit(1)


fitz   = _import_or_exit("fitz")            # PyMuPDF
Image  = _import_or_exit("PIL.Image")          # Pillow — keep as the module, not PIL.Image.Image
OpenAI = _import_or_exit("openai", "OpenAI")

# ── Configuration ──────────────────────────────────────────────────────────────

DEFAULT_SCAN_DIR      = Path("~/Pictures/Scanned Docs").expanduser()
PROCESSED_TAG         = "AI-Processed"
FAILED_TAG            = "AI-Failed"
UNPROCESSED_PREFIXES  = ("SCAN_", "IMG_")
SUPPORTED_EXTENSIONS  = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"}
OPENAI_MODEL          = "gpt-4o"
IMAGE_MAX_PX          = 1568   # longest-edge limit sent to the API
IMAGE_JPEG_QUALITY    = 85

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── macOS extended-attribute helpers (pure Python via ctypes) ──────────────────
# Uses macOS getxattr / setxattr from libSystem — no pip packages required.

_libc = ctypes.CDLL(None)   # libSystem.B.dylib is already in the process
_libc.getxattr.restype  = ctypes.c_ssize_t
_libc.setxattr.restype  = ctypes.c_int

_TAGS_ATTR = "com.apple.metadata:_kMDItemUserTags"

# ───────────────────────────────────────────────────────────────────────────────
# macOS extended-attribute helpers (pure Python via ctypes)
# Uses macOS getxattr / setxattr from libSystem — no pip packages required.

def _xattr_get(path: Path, attr: str) -> bytes | None:
    """Return raw bytes of an extended attribute, or None if absent."""
    p = str(path).encode()
    a = attr.encode()
    size = _libc.getxattr(p, a, None, 0, 0, 0)
    if size < 0:
        return None
    buf = ctypes.create_string_buffer(size)
    ret = _libc.getxattr(p, a, buf, ctypes.c_size_t(size), 0, 0)
    return bytes(buf) if ret >= 0 else None

# ──────────────────────────────────────────────────────────────────────────────
# macOS extended-attribute helpers (pure Python via ctypes)
# Uses macOS getxattr / setxattr from libSystem — no pip packages required.

def _xattr_set(path: Path, attr: str, value: bytes) -> None:
    """Write raw bytes to an extended attribute."""
    ret = _libc.setxattr(
        str(path).encode(),
        attr.encode(),
        value,
        ctypes.c_size_t(len(value)),
        0,
        0,
    )
    if ret != 0:
        raise OSError(f"setxattr failed on {path.name}")

# ─────────────────────────────────────────────────────────────────────────────
# macOS extended-attribute helpers (pure Python via ctypes)
# Uses macOS getxattr / setxattr from libSystem — no pip packages required.
def get_tags(path: Path) -> list[str]:
    """Return the macOS Finder tag names for *path*."""
    raw = _xattr_get(path, _TAGS_ATTR)
    if not raw:
        return []
    try:
        entries = plistlib.loads(raw)
        if not isinstance(entries, list):
            return []
        # Each entry may be "TagName\n<colour-int>" — keep only the name part.
        return [str(e).split("\n")[0] for e in entries]
    except Exception:
        return []

# ────────────────────────────────────────────────────────────────────────────
# macOS extended-attribute helpers (pure Python via ctypes)
# Uses macOS getxattr / setxattr from libSystem — no pip packages required

def add_tags(path: Path, *tags: str) -> None:
    """Append one or more Finder tags to *path*, avoiding duplicates."""
    raw = _xattr_get(path, _TAGS_ATTR)
    if raw:
        try:
            current: list = plistlib.loads(raw)
            if not isinstance(current, list):
                current = []
        except Exception:
            current = []
    else:
        current = []

    existing_names = {str(e).split("\n")[0] for e in current}
    for tag in tags:
        if tag not in existing_names:
            current.append(tag)
            existing_names.add(tag)

    _xattr_set(path, _TAGS_ATTR, plistlib.dumps(current, fmt=plistlib.FMT_BINARY))


def is_processed(path: Path) -> bool:
    """Return True if *path* has already been tagged as processed."""
    return PROCESSED_TAG in get_tags(path)


# ── Image extraction ───────────────────────────────────────────────────────────

def render_first_page(path: Path) -> bytes:
    """
    Render the first page of a PDF (or the image itself) as a JPEG,
    scaled so the longest edge is at most IMAGE_MAX_PX pixels.
    Returns JPEG bytes.
    """
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        doc  = fitz.open(str(path))
        page = doc[0]
        # 150 dpi gives good OCR quality without being excessive
        mat = fitz.Matrix(150 / 72, 150 / 72)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        doc.close()
    else:
        img = Image.open(path).convert("RGB")

    w, h = img.size
    if max(w, h) > IMAGE_MAX_PX:
        scale = IMAGE_MAX_PX / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=IMAGE_JPEG_QUALITY)
    return buf.getvalue()


# ── OpenAI Vision analysis ─────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a document classification assistant. You are shown the first page of a \
scanned document. Identify what it is and suggest a clean, descriptive filename.

Respond ONLY with a JSON object — no markdown fences, no extra text:
{
  "filename": "Descriptive File Name",
  "category": "Category"
}

filename rules:
- No file extension
- Title Case
- Specific but concise (e.g. "British Passport", "NatWest Bank Statement Oct 2024",
  "PCN Penalty Charge Notice", "NHS Prescription Nasal Spray Jul 2024")
- Include visible dates, names, or reference numbers where useful
- 60 characters maximum

category — choose exactly one of:
  Identity | Financial | Medical | Legal | Insurance | Utility | Receipt |
  Government | Employment | Education | Property | Correspondence | Other
"""


def analyse_with_openai(client, image_bytes: bytes) -> dict:
    """Send image bytes to gpt-4o Vision and return {'filename':…, 'category':…}."""
    b64 = base64.standard_b64encode(image_bytes).decode()
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64}",
                            "detail": "high",
                        },
                    }
                ],
            },
        ],
        max_tokens=200,
        temperature=0,
    )
    raw = response.choices[0].message.content or ""
    raw = raw.strip()

    # Locate the JSON object by finding the outermost { … } — this is more
    # robust than stripping markdown fences, and handles extra prose around it.
    start = raw.find("{")
    end   = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise json.JSONDecodeError(
            f"No JSON object found in response. Raw reply was: {raw!r}", raw, 0
        )
    return json.loads(raw[start : end + 1])


# ── File-rename helpers ────────────────────────────────────────────────────────

_UNSAFE_CHARS = re.compile(r'[/:*?"<>|\\]')


def sanitise(name: str) -> str:
    """Remove characters that are illegal in macOS filenames."""
    name = _UNSAFE_CHARS.sub("", name).strip(". ")
    return name or "Untitled Document"


def unique_path(target: Path) -> Path:
    """Return *target*, appending (2), (3) … if it already exists."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    n = 2
    while True:
        candidate = target.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


# ── Core processing ────────────────────────────────────────────────────────────

def find_candidates(scan_dir: Path) -> list[Path]:
    """Return all unprocessed-looking files in *scan_dir*."""
    return sorted(
        p
        for p in scan_dir.iterdir()
        if p.is_file()
        and p.suffix.lower() in SUPPORTED_EXTENSIONS
        and any(p.name.startswith(prefix) for prefix in UNPROCESSED_PREFIXES)
    )


def process_file(client, path: Path, dry_run: bool) -> bool:
    """
    Analyse one file, rename it, and apply Finder tags.
    Returns True on success (or skip), False on error.
    """
    if is_processed(path):
        log.info("SKIP  (already tagged)  %s", path.name)
        return True

    log.info("SCAN  %s", path.name)

    try:
        image_bytes = render_first_page(path)
    except Exception as exc:
        log.error("      Could not render '%s': %s", path.name, exc)
        return False

    try:
        result = analyse_with_openai(client, image_bytes)
    except json.JSONDecodeError as exc:
        log.error("      Bad JSON from OpenAI for '%s': %s", path.name, exc)
        if not dry_run:
            add_tags(path, PROCESSED_TAG, FAILED_TAG)
            log.info("      Tagged '%s' as %s + %s", path.name, PROCESSED_TAG, FAILED_TAG)
        return False
    except Exception as exc:
        log.error("      OpenAI error for '%s': %s", path.name, exc)
        if not dry_run:
            add_tags(path, PROCESSED_TAG, FAILED_TAG)
            log.info("      Tagged '%s' as %s + %s", path.name, PROCESSED_TAG, FAILED_TAG)
        return False

    raw_name = result.get("filename", "").strip()
    category  = result.get("category", "Other").strip()

    if not raw_name:
        log.warning("      No filename returned for '%s', skipping.", path.name)
        if not dry_run:
            add_tags(path, PROCESSED_TAG, FAILED_TAG)
            log.info("      Tagged '%s' as %s + %s", path.name, PROCESSED_TAG, FAILED_TAG)
        return False

    new_name = sanitise(raw_name) + path.suffix.lower()
    new_path = unique_path(path.parent / new_name)

    log.info("   →  %s  [%s]", new_name, category)

    if not dry_run:
        try:
            path.rename(new_path)
        except OSError as exc:
            log.error("      Rename failed for '%s': %s", path.name, exc)
            return False
        try:
            add_tags(new_path, PROCESSED_TAG, category)
        except OSError as exc:
            log.warning("      Tags not applied to '%s': %s", new_name, exc)
            # Not fatal — file was renamed successfully

    return True


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rename scanned documents using OpenAI Vision."
    )
    parser.add_argument(
        "--dir",
        default=str(DEFAULT_SCAN_DIR),
        metavar="PATH",
        help="Directory containing scans (default: same directory as this script)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without renaming or tagging any files",
    )
    args = parser.parse_args()

    scan_dir = Path(args.dir).expanduser().resolve()
    if not scan_dir.is_dir():
        log.error("Not a directory: %s", scan_dir)
        sys.exit(1)

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        log.error(
            "OPENAI_API_KEY environment variable is not set.\n"
            "Export it before running:  export OPENAI_API_KEY='sk-...'"
        )
        sys.exit(1)

    client     = OpenAI(api_key=api_key)
    candidates = find_candidates(scan_dir)

    if not candidates:
        log.info("No unprocessed files found (nothing starting with %s).",
                 " or ".join(UNPROCESSED_PREFIXES))
        return

    log.info("Found %d file(s) to process in: %s", len(candidates), scan_dir)
    if args.dry_run:
        log.info("DRY RUN — no files will be renamed or tagged.")

    succeeded = failed = 0
    for path in candidates:
        if process_file(client, path, args.dry_run):
            succeeded += 1
        else:
            failed += 1

    log.info(
        "Done. %d succeeded%s.",
        succeeded,
        f", {failed} failed" if failed else "",
    )


if __name__ == "__main__":
    main()
