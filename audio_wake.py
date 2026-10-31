"""読み上げ前に音声出力を起こしておく。

Bluetooth 機器(Shokz OpenComm2 で確認)では、無音の状態から鳴らすと開始直後に
出力が 0.2〜0.9 秒止まり、読み始めがブツブツ切れることがある。そこで SAPI と同じ
waveOut の既定出力へ無音を流し、再生位置が進み出してから読み上げを始める。
止まらない機器でも待ちは 0.1 秒ほどで済む。
"""
import ctypes
import time
from ctypes import wintypes

WAVE_MAPPER = 0xFFFFFFFF  # 既定の出力先(切り替えにも追従する)
WHDR_BEGINLOOP = 4
WHDR_ENDLOOP = 8
TIME_BYTES = 4
MMSYSERR_NOERROR = 0

SAMPLE_RATE = 22050
BYTES_PER_SEC = SAMPLE_RATE * 2  # 16bit モノラル
READY_BYTES = BYTES_PER_SEC // 10  # 再生位置が 100ms 進めば停止は抜けている(実測)
WAIT_LIMIT = 1.5  # 実測の最大 0.9 秒に余裕を持たせた上限。超えたら待たずに読み始める
POLL_INTERVAL = 0.005


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD)]


class WAVEHDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD),
                ("dwBytesRecorded", wintypes.DWORD), ("dwUser", ctypes.c_size_t),
                ("dwFlags", wintypes.DWORD), ("dwLoops", wintypes.DWORD),
                ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_size_t)]


class MMTIME(ctypes.Structure):
    _fields_ = [("wType", wintypes.UINT), ("cb", wintypes.DWORD), ("pad", wintypes.DWORD)]


def _load_winmm():
    winmm = ctypes.WinDLL("winmm")
    handle = ctypes.c_void_p
    hdr_ptr = ctypes.POINTER(WAVEHDR)
    signatures = {
        "waveOutOpen": [ctypes.POINTER(handle), wintypes.UINT, ctypes.POINTER(WAVEFORMATEX),
                        ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD],
        "waveOutPrepareHeader": [handle, hdr_ptr, wintypes.UINT],
        "waveOutWrite": [handle, hdr_ptr, wintypes.UINT],
        "waveOutUnprepareHeader": [handle, hdr_ptr, wintypes.UINT],
        "waveOutGetPosition": [handle, ctypes.POINTER(MMTIME), wintypes.UINT],
        "waveOutReset": [handle],
        "waveOutClose": [handle],
    }
    for name, argtypes in signatures.items():
        func = getattr(winmm, name)
        func.argtypes = argtypes
        func.restype = wintypes.UINT
    return winmm


class OutputWake:
    """既定の出力へ無音を流し続ける。失敗しても読み上げは止めない。"""

    def __init__(self, winmm=None):
        self._winmm = winmm
        self._handle = None
        self._hdr = None
        self._ready = False
        self._silence = ctypes.create_string_buffer(BYTES_PER_SEC)  # 1 秒の無音をループする

    @property
    def is_open(self):
        return self._handle is not None

    def ensure_awake(self, cancelled=lambda: False):
        """出力が動き出すまで待つ。cancelled が真になれば待つのをやめる。"""
        if self.is_open and self._position() is None:
            self.close()  # 機器の切断などで無効になった
        if not self.is_open and not self._open():
            return
        deadline = time.monotonic() + WAIT_LIMIT
        while not self._ready and time.monotonic() < deadline and not cancelled():
            position = self._position()
            if position is None:
                self.close()
                return
            if position >= READY_BYTES:
                self._ready = True
                return
            time.sleep(POLL_INTERVAL)

    def close(self):
        if not self.is_open:
            return
        winmm, handle, hdr = self._winmm, self._handle, self._hdr
        self._handle = self._hdr = None
        self._ready = False
        winmm.waveOutReset(handle)
        if hdr is not None:
            winmm.waveOutUnprepareHeader(handle, ctypes.byref(hdr), ctypes.sizeof(hdr))
        winmm.waveOutClose(handle)

    def _open(self):
        if self._winmm is None:
            try:
                self._winmm = _load_winmm()
            except OSError:
                return False
        winmm = self._winmm
        handle = ctypes.c_void_p()
        fmt = WAVEFORMATEX(1, 1, SAMPLE_RATE, BYTES_PER_SEC, 2, 16, 0)
        if winmm.waveOutOpen(ctypes.byref(handle), WAVE_MAPPER, ctypes.byref(fmt),
                             0, 0, 0) != MMSYSERR_NOERROR:
            return False
        self._handle = handle
        hdr = WAVEHDR()
        hdr.lpData = ctypes.addressof(self._silence)
        hdr.dwBufferLength = len(self._silence)
        hdr.dwFlags = WHDR_BEGINLOOP | WHDR_ENDLOOP
        hdr.dwLoops = 0xFFFFFFFF
        size = ctypes.sizeof(hdr)
        if winmm.waveOutPrepareHeader(handle, ctypes.byref(hdr), size) != MMSYSERR_NOERROR:
            self.close()
            return False
        self._hdr = hdr
        if winmm.waveOutWrite(handle, ctypes.byref(hdr), size) != MMSYSERR_NOERROR:
            self.close()
            return False
        return True

    def _position(self):
        """再生済みバイト数を返す(出力が無効なら None)。"""
        mm = MMTIME(TIME_BYTES, 0, 0)
        result = self._winmm.waveOutGetPosition(self._handle, ctypes.byref(mm),
                                                ctypes.sizeof(mm))
        return mm.cb if result == MMSYSERR_NOERROR else None
