"""Download CC0 call-center ambience audio to app/assets/audio/.

These clips are mixed under the agent's voice output to make calls feel
realistic. All sources are CC0 (public-domain) from the SSE Library: AMBIENCE
collection on archive.org.

Usage
-----
    source venv/bin/activate
    python -m app.scripts.download_ambience_audio

Safe to re-run — existing files are skipped. Pass ``--force`` to redownload.
"""
from __future__ import annotations

import argparse
import sys
import urllib.parse
import urllib.request
from pathlib import Path


# (local_filename, archive.org URL). Add new clips here to ship them with the
# product. Filenames are stable across runs; the voice mixer picks one at random.
AMBIENCE_SOURCES: list[tuple[str, str]] = [
    (
        "office_movement_walla.mp3",
        "https://archive.org/download/SSE_Library_AMBIENCE/OFFICE/"
        + urllib.parse.quote("AMBOffc_Movement in indoor space; office or waiting_CS_USC.mp3"),
    ),
    (
        "office_small_objects.mp3",
        "https://archive.org/download/SSE_Library_AMBIENCE/OFFICE/"
        + urllib.parse.quote("AMBOffc_Manipulating small objects indoors; room_CS_USC.mp3"),
    ),
]

OUTPUT_DIR = Path(__file__).resolve().parents[1] / "assets" / "audio" / "call_center_ambience"


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "NokvoOne/1.0 (assets-bootstrap)"})
    with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as fh:
        fh.write(resp.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--force",
        action="store_true",
        help="Redownload even if files already exist.",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Saving to {OUTPUT_DIR}")

    failures = 0
    for filename, url in AMBIENCE_SOURCES:
        dest = OUTPUT_DIR / filename
        if dest.exists() and not args.force:
            print(f"  skip   {filename}  ({dest.stat().st_size} bytes already on disk)")
            continue
        print(f"  fetch  {filename}")
        try:
            _download(url, dest)
            print(f"         -> {dest.stat().st_size} bytes")
        except Exception as exc:
            print(f"         FAILED: {exc!r}")
            failures += 1

    if failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
