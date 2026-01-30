"""Chapter file parser.

Reads a simple text format with alternating START / TITLE lines::

    START=00:09:00.368000
    TITLE=Sycamore Grove
    START=00:13:00.150000
    TITLE=Bachelor of the Year
    ...

BUG FIX: The old parser computed seconds as
``(60 * t.minute) + (60 * 60 * t.hour)`` — omitting ``t.second``.
This happened to work because all existing chapter files had seconds=0,
but would silently produce wrong offsets for any timestamp where the
seconds component is nonzero.  Fixed here.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from .models import ChapterEntry


def parse_chapters(chapter_file: Path) -> list[ChapterEntry]:
    """Parse a chapter file and return a list of ``ChapterEntry`` objects.

    Raises ``FileNotFoundError`` if *chapter_file* does not exist.
    """
    chapters: list[ChapterEntry] = []

    with chapter_file.open("r") as f:
        while True:
            time_line = f.readline()
            if not time_line:
                break
            title_line = f.readline()
            if not title_line:
                break

            time_str = time_line.strip().split("=", 1)[1]
            title = title_line.strip().split("=", 1)[1]

            t = datetime.datetime.strptime(time_str, "%H:%M:%S.%f")
            seconds = (
                t.hour * 3600
                + t.minute * 60
                + t.second
                + t.microsecond / 1_000_000
            )
            chapters.append(ChapterEntry(title=title, start_seconds=seconds))

    return chapters
