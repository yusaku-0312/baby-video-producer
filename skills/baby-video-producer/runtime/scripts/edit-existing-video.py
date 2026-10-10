#!/usr/bin/env python3
"""Local Remotion editing entry point; credentials and generation APIs are unused."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from baby_video.edit import edit_existing
from baby_video.state import PipelineError

parser = argparse.ArgumentParser(description='既存の縦動画に短い字幕とBGMを付ける')
parser.add_argument('--video', type=Path, required=True)
parser.add_argument('--captions', type=Path, required=True)
parser.add_argument('--bgm', type=Path)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--settings', type=Path)
args = parser.parse_args()
try:
    settings = json.loads(args.settings.read_text()) if args.settings else None
    print(json.dumps(edit_existing(args.video, args.captions, args.bgm, args.output, settings),
                     ensure_ascii=False, indent=2))
except (PipelineError, OSError, ValueError) as error:
    print(json.dumps({'error': str(error), 'live_api_called': False}, ensure_ascii=False))
    sys.exit(2)
