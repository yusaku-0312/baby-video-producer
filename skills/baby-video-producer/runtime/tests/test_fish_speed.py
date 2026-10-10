import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from baby_video.providers import fish_tts
from baby_video.state import PipelineError


class FishSpeedTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.destination = Path(directory.name) / "voice.mp3"
        environment = patch.dict("os.environ", {"FISH_API_KEY": "TEST-ONLY"})
        environment.start()
        self.addCleanup(environment.stop)

    def response(self):
        response = io.BytesIO(b"MOCK AUDIO")
        response.headers = {"Content-Type": "audio/mpeg"}
        return response

    def test_default_generation_uses_nested_prosody_speed_110(self):
        with patch("baby_video.providers.urllib.request.urlopen", return_value=self.response()) as send:
            fish_tts("テスト", "mock-voice", self.destination, model="s2.1-pro-free")
        request = send.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload.get("prosody"), {"speed": 1.1})
        self.assertNotIn("speed", {key: value for key, value in payload.items() if key != "prosody"})
        self.assertEqual(request.get_header("Model"), "s2.1-pro-free")
        self.assertEqual(self.destination.read_bytes(), b"MOCK AUDIO")

    def test_explicit_speed_and_api_boundaries_are_forwarded(self):
        for speed in (0.5, 1.0, 1.25, 1.3, 2.0):
            with self.subTest(speed=speed), patch(
                    "baby_video.providers.urllib.request.urlopen", return_value=self.response()) as send:
                fish_tts("テスト", "mock-voice", self.destination, speed=speed)
                self.assertEqual(json.loads(send.call_args.args[0].data)["prosody"]["speed"], speed)

    def test_invalid_speeds_never_submit(self):
        for speed in (None, True, "1.25", 0.49, 2.01, float("nan"), float("inf")):
            with self.subTest(speed=speed), patch("baby_video.providers.urllib.request.urlopen") as send:
                with self.assertRaises(PipelineError):
                    fish_tts("テスト", "mock-voice", self.destination, speed=speed)
                send.assert_not_called()

    def test_failure_does_not_retry_or_replace_existing_audio(self):
        self.destination.write_bytes(b"EXISTING AUDIO")
        with patch("baby_video.providers.urllib.request.urlopen", side_effect=OSError("MOCK")) as send:
            with self.assertRaises(PipelineError):
                fish_tts("テスト", "mock-voice", self.destination)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.destination.read_bytes(), b"EXISTING AUDIO")
        self.assertFalse(self.destination.with_suffix(".part.mp3").exists())
