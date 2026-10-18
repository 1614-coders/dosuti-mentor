"""Capacity observations are advisory except the known disk lower bound."""
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import resource_preflight as resources


class ResourcePreflight(unittest.TestCase):
    def inspect(self, free, ram=None, **changes):
        options = dict(audio_needed=True, recognition_needed=True, pending=True)
        options.update(changes)
        with patch.object(resources.shutil, 'disk_usage', return_value=NS(free=free)), \
                patch.object(resources, 'available_memory', return_value=ram):
            return resources.inspect(Path('.'), 7200, **options)

    def test_two_hour_pcm_is_lower_bound_and_blocks_shortfall(self):
        data = self.inspect(100 * resources.MIB)
        self.assertEqual(data['new_audio_bytes'], 230400044)
        self.assertTrue(data['blocked'])
        with self.assertRaisesRegex(ValueError, 'Кадры потребуют дополнительное место'):
            resources.enforce(data)

    def test_boundary_and_cached_audio(self):
        data = self.inspect(230400044 + resources.RESERVE)
        self.assertFalse(data['blocked'])
        cached = self.inspect(resources.RESERVE, audio_needed=False)
        self.assertEqual(cached['new_audio_bytes'], 0)
        self.assertFalse(cached['blocked'])

    def test_low_ram_warns_without_blocking_or_selecting_another_model(self):
        data = self.inspect(10**12, ram=resources.MIB)
        self.assertFalse(data['blocked'])
        self.assertIn('Качество распознавания не изменено', data['warnings'][0])
        self.assertNotIn('model', data)

    def test_unknown_counters_are_not_zero(self):
        with patch.object(resources.shutil, 'disk_usage', side_effect=OSError('unavailable')), \
                patch.object(resources, 'available_memory', return_value=None):
            data = resources.inspect(Path('.'), 60, audio_needed=True,
                                     recognition_needed=True, pending=True)
        self.assertIsNone(data['disk_free_bytes'])
        self.assertIsNone(data['available_ram_bytes'])
        self.assertFalse(data['blocked'])
        self.assertEqual(len(data['warnings']), 1)

    def test_finished_job_needs_no_new_payload_and_no_ram_probe(self):
        with patch.object(resources, 'available_memory', side_effect=AssertionError('No model to load')), \
                patch.object(resources.shutil, 'disk_usage', return_value=NS(free=0)):
            data = resources.inspect(Path('.'), 60, audio_needed=False,
                                     recognition_needed=False, pending=False)
        self.assertFalse(data['blocked'])
        self.assertEqual(data['reserve_bytes'], 0)

    def test_unsupported_native_counters_return_unknown(self):
        with patch('resource_usage.WindowsProbe', side_effect=OSError('unsupported')):
            self.assertIsNone(resources.available_memory())

    def test_memory_probe_does_not_enumerate_processes(self):
        import ctypes
        class Memory(ctypes.Structure):
            _fields_ = [('size', ctypes.c_ulong), ('available', ctypes.c_ulonglong)]
        def sample(pointer):
            self.assertEqual(pointer._obj.size, ctypes.sizeof(Memory))
            pointer._obj.available = 123456
            return True
        probe = NS(SystemMemory=Memory, k=NS(GlobalMemoryStatusEx=sample))
        with patch('resource_usage.WindowsProbe', return_value=probe):
            self.assertEqual(resources.available_memory(), 123456)
