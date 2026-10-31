import unittest
from unittest import mock

import audio_wake
from audio_wake import READY_BYTES, OutputWake


class FakeWinmm:
    """waveOut API の代役。positions を順に返し、尽きたら最後の値を返し続ける。"""

    def __init__(self, positions=(READY_BYTES,), open_result=0, write_result=0):
        self.positions = list(positions)
        self.open_result = open_result
        self.write_result = write_result
        self.opened = 0
        self.closed = 0

    def waveOutOpen(self, handle, device, fmt, callback, instance, flags):
        self.opened += 1
        return self.open_result

    def waveOutPrepareHeader(self, handle, hdr, size):
        return 0

    def waveOutWrite(self, handle, hdr, size):
        return self.write_result

    def waveOutGetPosition(self, handle, mm, size):
        value = self.positions.pop(0) if len(self.positions) > 1 else self.positions[0]
        if value is None:
            return 2  # MMSYSERR_BADDEVICEID など
        mm._obj.cb = value
        return 0

    def waveOutReset(self, handle):
        return 0

    def waveOutUnprepareHeader(self, handle, hdr, size):
        return 0

    def waveOutClose(self, handle):
        self.closed += 1
        return 0


class OutputWakeTests(unittest.TestCase):
    def test_waits_until_position_advances(self):
        winmm = FakeWinmm([0, 0, 10, READY_BYTES])
        wake = OutputWake(winmm)
        wake.ensure_awake()
        self.assertTrue(wake.is_open)
        self.assertEqual([READY_BYTES], winmm.positions)

    def test_second_call_reuses_the_running_output(self):
        winmm = FakeWinmm()
        wake = OutputWake(winmm)
        wake.ensure_awake()
        wake.ensure_awake()
        self.assertEqual(1, winmm.opened)

    def test_gives_up_at_the_wait_limit_without_failing(self):
        wake = OutputWake(FakeWinmm([0]))
        with mock.patch.object(audio_wake, "WAIT_LIMIT", 0.02):
            wake.ensure_awake()
        self.assertTrue(wake.is_open)

    def test_cancel_stops_waiting(self):
        winmm = FakeWinmm([0, 0, 0, 0, 0, 0])
        OutputWake(winmm).ensure_awake(lambda: True)
        self.assertEqual(6, len(winmm.positions))

    def test_open_failure_is_harmless(self):
        wake = OutputWake(FakeWinmm(open_result=2))
        wake.ensure_awake()
        self.assertFalse(wake.is_open)

    def test_write_failure_releases_the_device(self):
        winmm = FakeWinmm(write_result=2)
        wake = OutputWake(winmm)
        wake.ensure_awake()
        self.assertFalse(wake.is_open)
        self.assertEqual(1, winmm.closed)

    def test_lost_device_is_reopened(self):
        winmm = FakeWinmm([READY_BYTES, None, READY_BYTES])
        wake = OutputWake(winmm)
        wake.ensure_awake()
        wake.ensure_awake()
        self.assertEqual(2, winmm.opened)
        self.assertEqual(1, winmm.closed)

    def test_close_is_idempotent(self):
        winmm = FakeWinmm()
        wake = OutputWake(winmm)
        wake.ensure_awake()
        wake.close()
        wake.close()
        self.assertEqual(1, winmm.closed)


if __name__ == "__main__":
    unittest.main()
