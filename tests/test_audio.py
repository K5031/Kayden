import os
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from kayden import audio


def _sounddevice(devices, default=(0, 0)):
    module = ModuleType("sounddevice")
    module.query_devices = lambda: devices
    module.default = SimpleNamespace(device=default)
    return module


class _PyAudio:
    def __init__(self, devices, default=None):
        self.devices = devices
        self.default = default
        self.terminated = False

    def get_device_count(self):
        return len(self.devices)

    def get_device_info_by_index(self, index):
        return self.devices[index]

    def get_default_input_device_info(self):
        if self.default is None:
            raise OSError("no default")
        return {"index": self.default}

    def terminate(self):
        self.terminated = True


def _pyaudio(instance):
    module = ModuleType("pyaudio")
    module.PyAudio = lambda: instance
    return module


class AudioTests(unittest.TestCase):
    def setUp(self):
        audio._initialized = False

    def test_output_matches_realistic_device_names(self):
        sounddevice = _sounddevice([
            {"name": "Built-in Audio", "max_output_channels": 2},
            {"name": "pulse", "max_output_channels": 32},
        ])
        with patch.dict(sys.modules, {"sounddevice": sounddevice}):
            self.assertEqual(audio.resolve_output_device(), 1)

    def test_output_can_be_selected_by_environment(self):
        sounddevice = _sounddevice([
            {"name": "Laptop Speakers", "max_output_channels": 2},
            {"name": "USB Headset", "max_output_channels": 2},
        ])
        with patch.dict(sys.modules, {"sounddevice": sounddevice}), patch.dict(
            os.environ, {"KAYDEN_OUTPUT_DEVICE": "headset"}
        ):
            self.assertEqual(audio.resolve_output_device(), 1)

    def test_input_uses_pyaudio_without_touching_sounddevice(self):
        instance = _PyAudio([
            {"name": "USB Microphone", "maxInputChannels": 1},
        ])
        broken_sounddevice = ModuleType("sounddevice")
        with patch.dict(sys.modules, {
            "pyaudio": _pyaudio(instance),
            "sounddevice": broken_sounddevice,
        }):
            self.assertEqual(audio.resolve_pyaudio_input_device(), 0)
        self.assertTrue(instance.terminated)

    def test_missing_devices_produce_actionable_errors(self):
        sounddevice = _sounddevice([], default=(-1, -1))
        instance = _PyAudio([])
        with patch.dict(sys.modules, {
            "sounddevice": sounddevice,
            "pyaudio": _pyaudio(instance),
        }):
            with self.assertRaisesRegex(RuntimeError, "No usable audio output"):
                audio.resolve_output_device()
            with self.assertRaisesRegex(RuntimeError, "No usable microphone"):
                audio.resolve_pyaudio_input_device()


if __name__ == "__main__":
    unittest.main()
