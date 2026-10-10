"""Concrete provider adapters. No provider calls happen on import."""
from __future__ import annotations

import json
import math
import os
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .state import PipelineError

PROJECT = Path(__file__).resolve().parent.parent
H3_MODEL = "minimax/h3-max-turbo/image-to-video"
KLING_MODEL = "fal-ai/kling-video/lipsync/audio-to-video"
DEFAULT_FISH_SPEED = 1.1


def validate_fish_speed(speed) -> float:
    if (isinstance(speed, bool) or not isinstance(speed, (int, float))
            or not math.isfinite(speed) or not 0.5 <= speed <= 2.0):
        raise PipelineError("Fish speed must be a finite number between 0.5 and 2.0")
    return float(speed)


def fish_tts_payload(text: str, voice_id: str, *, speed=DEFAULT_FISH_SPEED) -> dict:
    return {"text": text, "reference_id": voice_id, "format": "mp3",
            "sample_rate": 44100, "mp3_bitrate": 128,
            "prosody": {"speed": validate_fish_speed(speed)}}


def fish_tts(text: str, voice_id: str, destination: Path, *, model="s2.1-pro",
             speed=DEFAULT_FISH_SPEED) -> None:
    payload = fish_tts_payload(text, voice_id, speed=speed)
    credential = os.environ.get("FISH_API_KEY")
    if not credential or not voice_id:
        raise PipelineError("Fish environment credential and selected voice ID are required")
    if model not in {"s1", "s2-pro", "s2.1-pro", "s2.1-pro-free", "drama-3-preview"}:
        raise PipelineError("Unknown Fish engine; do not allow fallback to a different paid model")
    body = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request("https://api.fish.audio/v1/tts", data=body, method="POST",
        headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json", "model": model})
    temporary = destination.with_suffix(".part.mp3")
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            # Refuse accidental JSON error payloads; do not log their content.
            if "json" in response.headers.get("Content-Type", ""):
                raise PipelineError("Fish returned no usable audio")
            data = response.read(10_000_001)
        if not data or len(data) > 10_000_000:
            raise PipelineError("Fish audio response is missing or exceeds the safe size limit")
        temporary.write_bytes(data)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise PipelineError("Fish request outcome needs review before another TTS submission") from None


class Fal:
    def __init__(self, model: str, expiration_seconds=3600):
        if model not in {H3_MODEL, KLING_MODEL}:
            raise PipelineError("Selected model has no verified adapter")
        self.model = model
        self.expiration = expiration_seconds

    def call(self, operation: str, **kwargs) -> dict:
        request = {"operation": operation, "model": self.model,
                   "mediaPolicy": "public_expiring", "expirationSeconds": self.expiration, **kwargs}
        try:
            response = subprocess.run(["node", str(PROJECT / "scripts/fal-worker.mjs")],
                input=json.dumps(request).encode(), capture_output=True, timeout=90, cwd=PROJECT)
            result = json.loads(response.stdout)
            if response.returncode or "error" in result:
                raise ValueError()
            return result
        except (OSError, subprocess.SubprocessError, ValueError):
            raise PipelineError("fal operation unavailable; keep the existing request ID for recovery") from None

    def upload(self, file: Path, content_type: str) -> str:
        return self.call("upload", file=str(file.resolve()), contentType=content_type)["url"]

    def submit(self, inputs: dict) -> str:
        return self.call("submit", input=inputs)["request_id"]

    def status(self, request_id: str) -> dict:
        return self.call("status", request_id=request_id)

    def download_result(self, request_id: str, destination: Path) -> None:
        url = self.call("result", request_id=request_id)["url"]
        parsed = urlparse(url)
        # No API credential is sent to the CDN. Reject arbitrary hosts and redirects.
        allowed = parsed.hostname == "fal.media" or (parsed.hostname or "").endswith(".fal.media")
        allowed = allowed or parsed.hostname == "storage.googleapis.com"
        if parsed.scheme != "https" or not allowed:
            raise PipelineError("Provider output uses an unverified download host")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise PipelineError("Unexpected media redirect")
        temporary = destination.with_suffix(".part.mp4")
        try:
            opener = urllib.request.build_opener(NoRedirect)
            with opener.open(url, timeout=90) as response, temporary.open("wb") as stream:
                count = 0
                while chunk := response.read(1024 * 1024):
                    count += len(chunk)
                    if count > 200_000_000:
                        raise PipelineError("Video response exceeds the download limit")
                    stream.write(chunk)
            if not temporary.stat().st_size:
                raise PipelineError("Empty output video")
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise PipelineError("Output download failed; resume the saved request without resubmitting") from None
