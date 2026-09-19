"""Mirror the Pi's console state to this PC, for screenshots and screen recordings.

Run (two terminals on the PC; the live loop keeps running on the Pi only):
    python -m virta.mirror                       # copies ~/virta/var/state.json every 2 s
    python -m virta.console --state var/state_pi.json

Reads over SSH (key and host from VIRTA_PI_SSH_KEY / VIRTA_PI_HOST, defaults
~/.ssh/virta_pi and lilja@raspberrypi). Read-only: it never writes to the Pi, and
the console it feeds cannot act - the Pi's own loop does all the acting.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .fsutil import atomic_write_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=os.environ.get("VIRTA_PI_HOST", "lilja@raspberrypi"))
    parser.add_argument("--key", default=os.environ.get("VIRTA_PI_SSH_KEY", str(Path.home() / ".ssh" / "virta_pi")))
    parser.add_argument("--remote", default="virta/var/state.json", help="path on the Pi, relative to home")
    parser.add_argument("--out", default="var/state_pi.json")
    parser.add_argument("--every", type=float, default=2.0, help="seconds between copies")
    args = parser.parse_args(argv)

    command = ["ssh", "-i", args.key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", args.host, f"cat {args.remote}"]
    print(f"mirroring {args.host}:~/{args.remote} -> {args.out} every {args.every:g} s  (Ctrl+C to stop)")
    failures = 0
    try:
        while True:
            started = time.monotonic()
            try:
                raw = subprocess.run(command, capture_output=True, timeout=15, stdin=subprocess.DEVNULL).stdout
                json.loads(raw)  # only a complete snapshot replaces the last good one
                atomic_write_text(args.out, raw.decode("utf-8"))
                failures = 0
            except (subprocess.SubprocessError, ValueError, OSError) as exc:
                failures += 1
                if failures in (1, 10) or failures % 60 == 0:
                    print(f"{time.strftime('%H:%M:%S')}  copy failed ({type(exc).__name__}) - retrying", file=sys.stderr)
            time.sleep(max(0.0, args.every - (time.monotonic() - started)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
