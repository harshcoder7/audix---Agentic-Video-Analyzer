"""Auto-append peak-RAM measurements for pipeline stages to logs/resource_usage.md.

Uses stdlib `resource` (RUSAGE_SELF + RUSAGE_CHILDREN) so every ingestion run
self-logs without anyone remembering to wrap it in `time -l` by hand.

Caveat: RUSAGE_CHILDREN's ru_maxrss is a cumulative high-water mark for all
child processes reaped since this Python process started, not a per-stage
delta -- so later stages in the same run show the running max, not an
isolated number. Good enough as an upper bound; noted in the log line itself.
"""

import platform
import resource
import time
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "resource_usage.md"
_RSS_UNIT = 1024 if platform.system() == "Linux" else 1024 * 1024  # macOS reports bytes, Linux KB


def _peak_rss_mb() -> float:
    self_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    children_rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return max(self_rss, children_rss) / _RSS_UNIT


class track:
    """with track("label"): ... appends one row to logs/resource_usage.md on exit."""

    def __init__(self, label: str):
        self.label = label

    def __enter__(self):
        self._t0 = time.time()
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed = time.time() - self._t0
        peak_mb = _peak_rss_mb()
        status = "ok" if exc_type is None else f"FAILED ({exc})"
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(
                f"| {time.strftime('%Y-%m-%d')} | {self.label} | (in-process pipeline stage) "
                f"| cumulative peak RSS (self+children, high-water since process start): {peak_mb:.0f} MB "
                f"| {elapsed:.1f}s wall | {status}, auto-logged |\n"
            )
        return False
