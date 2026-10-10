import array
import math
import tempfile
import unittest
import wave
from pathlib import Path

import baby_video.media as media


class SpeechDurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def audio(self, regions, duration):
        path = self.root / 'voice.wav'
        samples = array.array('h', (
            int(10000 * math.sin(2 * math.pi * 440 * i / 16000))
            if any(start <= i / 16000 < end for start, end in regions) else 0
            for i in range(round(duration * 16000))))
        with wave.open(str(path), 'wb') as output:
            output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            output.writeframes(samples.tobytes())
        return path

    def test_five_second_voice_with_silent_padding_does_not_pass(self):
        result = media.check_speech_duration(self.audio([(0.1, 5.4)], 9.2))
        self.assertFalse(result['passed'])
        self.assertIn('speech_too_short', result['problems'])
        self.assertAlmostEqual(result['speech_span_seconds'], 5.3, places=2)

    def test_minimum_8_5_seconds_is_inclusive(self):
        result = media.check_speech_duration(self.audio([(0.1, 8.6)], 8.7))
        self.assertTrue(result['passed'])
        self.assertAlmostEqual(result['speech_span_seconds'], 8.5, places=2)

    def test_natural_sentence_pause_is_included_in_reading_span(self):
        result = media.check_speech_duration(self.audio([(0.1, 4.3), (4.6, 9.1)], 9.2))
        self.assertTrue(result['passed'])
        self.assertAlmostEqual(result['speech_span_seconds'], 9, places=2)
        self.assertEqual(len(result['audio_activity']), 2)

    def test_more_than_ten_seconds_and_silent_audio_are_rejected(self):
        result = media.check_speech_duration(self.audio([(0, 10.2)], 10.2))
        self.assertFalse(result['passed'])
        self.assertIn('speech_too_long', result['problems'])
        result = media.check_speech_duration(self.audio([], 9))
        self.assertFalse(result['passed'])
        self.assertIn('no_audio_activity', result['problems'])


if __name__ == '__main__':
    unittest.main()
