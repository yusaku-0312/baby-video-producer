#!/usr/bin/env python3
"""Prepare blank .env.local once; the owner edits it without sending keys."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from baby_video.credentials import prepare_template, credential_presence
from baby_video.state import PipelineError


def main():
    parser = argparse.ArgumentParser(description="Prepare owner-only blank local environment input")
    parser.add_argument("--provider", choices=("fal", "fish", "both"), default="both")
    parser.parse_args()
    try:
        created = prepare_template(ROOT)
        print(json.dumps({"status": "prepared" if created else "preserved", "input_file": str(ROOT / ".env.local"),
                          "credential_present": credential_presence(ROOT), "network_called": False}))
        return 0
    except (PipelineError, OSError):
        print(json.dumps({"error": "Local environment preparation stopped; no values were logged", "network_called": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
