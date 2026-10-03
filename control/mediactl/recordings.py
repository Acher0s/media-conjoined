"""Finding recorded segments of a team path on disk.

MediaMTX writes one file per segment, named after the segment's start time (UTC, see
mediamtx.yml: recordPath and TZ=UTC), each starting on a keyframe. A segment's end is taken
from its file's modification time: MediaMTX writes the last part of a segment just before it
starts the next one, so within one publishing session the next segment starts right where the
previous one's mtime is. A gap between them means the stream was down.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})-(\d{6})\.(mp4|ts)$")
# Two segments closer than this belong to the same continuous stream
GAP_TOLERANCE = 1.5


@dataclass(frozen=True)
class Segment:
    path: Path
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def parse_start(name: str) -> float | None:
    m = NAME_RE.match(name)
    if not m:
        return None
    y, mo, d, h, mi, s, us = (int(x) for x in m.groups()[:7])
    return datetime(y, mo, d, h, mi, s, us, tzinfo=timezone.utc).timestamp()


def segments(directory: Path) -> list[Segment]:
    """All segments in a path's recording directory, oldest first."""
    if not directory.is_dir():
        return []
    found = []
    for f in directory.iterdir():
        start = parse_start(f.name)
        if start is None:
            continue
        try:
            mtime = f.stat().st_mtime
        except FileNotFoundError:  # pruned meanwhile
            continue
        found.append((start, mtime, f))
    found.sort()
    result = []
    for i, (start, mtime, f) in enumerate(found):
        end = mtime
        if i + 1 < len(found):
            end = min(end, found[i + 1][0])
        result.append(Segment(f, start, max(end, start)))
    return result


def covering(segs: list[Segment], t: float) -> int | None:
    """Index of the segment that contains time t, or None if t falls in a gap."""
    for i, seg in enumerate(segs):
        if seg.start <= t < seg.end:
            return i
        if seg.start > t:
            return None
    return None


def contiguous_from(segs: list[Segment], i: int, stop_at: float | None = None, complete_before: float | None = None):
    """Segments from index i onwards while they're continuous, stopping before a segment that starts
    at or after `stop_at` and leaving out segments still being written (end after `complete_before`)."""
    run = []
    for seg in segs[i:]:
        if run and seg.start - run[-1].end > GAP_TOLERANCE:
            break
        if stop_at is not None and seg.start >= stop_at and run:
            break
        if complete_before is not None and seg.end > complete_before:
            break
        run.append(seg)
    return run


def overlapping(segs: list[Segment], start: float, end: float) -> list[Segment]:
    return [s for s in segs if s.end > start and s.start < end]
