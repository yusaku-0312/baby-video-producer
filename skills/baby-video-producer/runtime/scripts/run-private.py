#!/usr/bin/env python3
"""Load only this project's owner-entered credentials into one CLI process."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from baby_video.credentials import activate_credentials
from baby_video.state import PipelineError


def main():
    try:
        activate_credentials(ROOT)
        environment = dict(os.environ)
        os.chdir(ROOT)
        args = sys.argv[1:] or ["doctor"]
        os.execvpe(sys.executable, [sys.executable, "-m", "baby_video", *args], environment)
    except (PipelineError, OSError):
        print(json.dumps({"error": "Private credential loading failed; no values were logged"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
