"""
Per-process WASAPI audio loopback for Windows 10 2004+ (build >= 19041).

Windows Audio Engine intercepts audio rendered by a specific PID and writes
raw PCM (float32 LE, 48 kHz, stereo) to a Windows named pipe that FFmpeg
reads as an audio input.
"""
from __future__ import annotations

import sys
import ctypes
import ctypes.wintypes as wt
import threading
import time


def is_process_loopback_supported() -> bool:
    """True if Windows 10 2004+ (build >= 19041) and comtypes is importable."""
    if sys.platform != "win32":
        return False
    try:
        build = sys.getwindowsversion().build
        if build < 19041:
            return False
        import comtypes  # noqa: F401
        return True
    except Exception:
        return False


# ─── The rest of the module is only meaningful on Windows ────────────────────

if sys.platform != "win32":
    class ProcessAudioCapture:  # type: ignore[no-redef]
        def __init__(self, pid: int) -> None:
            raise RuntimeError("WASAPI process loopback only supported on Windows")
        def start(self) -> None: ...
        def stop(self) -> None: ...
        ffmpeg_audio_args: list[str] = []

else:
    import comtypes
    import comtypes.client
    from comtypes import GUID, IUnknown, COMMETHOD, HRESULT, COMObject, POINTER as CPOINTER

    # ─── GUIDs ───────────────────────────────────────────────────────────────

    _IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
    _IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _IID_IActivateAudioInterfaceAsyncOperation = GUID("{72A567CE-257A-49B2-A561-BFB2AE84E79F}")
    _IID_IActivateAudioInterfaceCompletionHandler = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")

    # ─── Structures ──────────────────────────────────────────────────────────

    class _WAVEFORMATEX(ctypes.Structure):
        _fields_ = [
            ("wFormatTag",      wt.WORD),
            ("nChannels",       wt.WORD),
            ("nSamplesPerSec",  wt.DWORD),
            ("nAvgBytesPerSec", wt.DWORD),
            ("nBlockAlign",     wt.WORD),
            ("wBitsPerSample",  wt.WORD),
            ("cbSize",          wt.WORD),
        ]

    class _AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
        _fields_ = [
            ("TargetProcessId",    wt.DWORD),
            # 0 = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
            ("ProcessLoopbackMode", wt.DWORD),
        ]

    class _AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
        _fields_ = [
            # 1 = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
            ("ActivationType",       wt.DWORD),
            ("ProcessLoopbackParams", _AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
        ]

    # Minimal PROPVARIANT (VT_BLOB only)
    class _BLOB(ctypes.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("pBlobData", ctypes.c_void_p)]

    class _PV_UNION(ctypes.Union):
        _fields_ = [("blob", _BLOB), ("_pad", ctypes.c_byte * 16)]

    class _PROPVARIANT(ctypes.Structure):
        _fields_ = [
            ("vt",         wt.WORD),
            ("wReserved1", wt.WORD),
            ("wReserved2", wt.WORD),
            ("wReserved3", wt.WORD),
            ("value",      _PV_UNION),
        ]

    _VT_BLOB = 0x0041

    # ─── COM interfaces ───────────────────────────────────────────────────────

    class _IActivateAudioInterfaceAsyncOperation(IUnknown):
        _iid_ = _IID_IActivateAudioInterfaceAsyncOperation
        _methods_ = [
            COMMETHOD([], HRESULT, "GetActivateResult",
                (["out"], ctypes.POINTER(ctypes.HRESULT),            "activateResult"),
                (["out"], ctypes.POINTER(ctypes.POINTER(IUnknown)),  "activatedInterface"),
            ),
        ]

    class _IActivateAudioInterfaceCompletionHandler(IUnknown):
        _iid_ = _IID_IActivateAudioInterfaceCompletionHandler
        _methods_ = [
            COMMETHOD([], HRESULT, "ActivateCompleted",
                (["in"], ctypes.POINTER(_IActivateAudioInterfaceAsyncOperation), "activateOperation"),
            ),
        ]

    _AUDCLNT_SHAREMODE_SHARED = 0
    _AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000

    class _IAudioClient(IUnknown):
        _iid_ = _IID_IAudioClient
        _methods_ = [
            COMMETHOD([], HRESULT, "Initialize",
                (["in"], wt.DWORD,                          "ShareMode"),
                (["in"], wt.DWORD,                          "StreamFlags"),
                (["in"], ctypes.c_longlong,                 "hnsBufferDuration"),
                (["in"], ctypes.c_longlong,                 "hnsPeriodicity"),
                (["in"], ctypes.POINTER(_WAVEFORMATEX),     "pFormat"),
                (["in"], ctypes.POINTER(GUID),              "AudioSessionGuid"),
            ),
            COMMETHOD([], HRESULT, "GetBufferSize",
                (["out"], ctypes.POINTER(wt.UINT), "pNumBufferFrames"),
            ),
            COMMETHOD([], HRESULT, "GetStreamLatency",
                (["out"], ctypes.POINTER(ctypes.c_longlong), "phnsLatency"),
            ),
            COMMETHOD([], HRESULT, "GetCurrentPadding",
                (["out"], ctypes.POINTER(wt.UINT), "pNumPaddingFrames"),
            ),
            COMMETHOD([], HRESULT, "IsFormatSupported",
                (["in"],  wt.DWORD,                                   "ShareMode"),
                (["in"],  ctypes.POINTER(_WAVEFORMATEX),              "pFormat"),
                (["out"], ctypes.POINTER(ctypes.POINTER(_WAVEFORMATEX)), "ppClosestMatch"),
            ),
            COMMETHOD([], HRESULT, "GetMixFormat",
                (["out"], ctypes.POINTER(ctypes.POINTER(_WAVEFORMATEX)), "ppDeviceFormat"),
            ),
            COMMETHOD([], HRESULT, "GetDevicePeriod",
                (["out"], ctypes.POINTER(ctypes.c_longlong), "phnsDefaultDevicePeriod"),
                (["out"], ctypes.POINTER(ctypes.c_longlong), "phnsMinimumDevicePeriod"),
            ),
            COMMETHOD([], HRESULT, "Start"),
            COMMETHOD([], HRESULT, "Stop"),
            COMMETHOD([], HRESULT, "Reset"),
            COMMETHOD([], HRESULT, "SetEventHandle",
                (["in"], wt.HANDLE, "eventHandle"),
            ),
            COMMETHOD([], HRESULT, "GetService",
                (["in"],  ctypes.POINTER(GUID),           "riid"),
                (["out"], ctypes.POINTER(ctypes.c_void_p), "ppv"),
            ),
        ]

    class _IAudioCaptureClient(IUnknown):
        _iid_ = _IID_IAudioCaptureClient
        _methods_ = [
            COMMETHOD([], HRESULT, "GetBuffer",
                (["out"], ctypes.POINTER(ctypes.POINTER(ctypes.c_byte)), "ppData"),
                (["out"], ctypes.POINTER(wt.UINT),                       "pNumFramesRead"),
                (["out"], ctypes.POINTER(wt.DWORD),                      "pdwFlags"),
                (["out"], ctypes.POINTER(ctypes.c_uint64),               "pu64DevicePosition"),
                (["out"], ctypes.POINTER(ctypes.c_uint64),               "pu64QPCPosition"),
            ),
            COMMETHOD([], HRESULT, "ReleaseBuffer",
                (["in"], wt.UINT, "NumFramesRead"),
            ),
            COMMETHOD([], HRESULT, "GetNextPacketSize",
                (["out"], ctypes.POINTER(wt.UINT), "pNumFramesInNextPacket"),
            ),
        ]

    # ─── Completion handler COM server ────────────────────────────────────────

    class _CompletionHandler(COMObject):
        _com_interfaces_ = [_IActivateAudioInterfaceCompletionHandler]

        def __init__(self) -> None:
            super().__init__()
            self._done = threading.Event()
            self._audio_client: _IAudioClient | None = None
            self._error: str | None = None

        def IActivateAudioInterfaceCompletionHandler_ActivateCompleted(
            self, activateOperation  # type: ignore[override]
        ) -> int:
            try:
                hr, iface = activateOperation.GetActivateResult()
                if hr != 0:
                    self._error = f"GetActivateResult HRESULT=0x{hr & 0xFFFFFFFF:08X}"
                else:
                    self._audio_client = iface.QueryInterface(_IAudioClient)
            except Exception as exc:
                self._error = str(exc)
            finally:
                self._done.set()
            return 0  # S_OK

        def wait(self, timeout: float = 5.0) -> bool:
            return self._done.wait(timeout)

    # ─── ActivateAudioInterfaceAsync ──────────────────────────────────────────

    _mmdevapi_fn = ctypes.windll.mmdevapi.ActivateAudioInterfaceAsync
    _mmdevapi_fn.restype = ctypes.HRESULT
    _mmdevapi_fn.argtypes = [
        wt.LPCWSTR,                               # deviceInterfacePath
        ctypes.POINTER(GUID),                     # riid (IAudioClient)
        ctypes.c_void_p,                          # activationParams (PROPVARIANT*)
        # Use comtypes POINTER so from_param() calls QueryInterface automatically —
        # avoids the null-pointer bug from manually accessing _com_pointers_.
        CPOINTER(_IActivateAudioInterfaceCompletionHandler),
        ctypes.POINTER(ctypes.c_void_p),          # **activationOperation (out)
    ]

    # ─── Named-pipe helpers ───────────────────────────────────────────────────

    _PIPE_ACCESS_OUTBOUND  = 0x00000002
    _PIPE_TYPE_BYTE        = 0x00000000
    _PIPE_READMODE_BYTE    = 0x00000000
    _PIPE_WAIT             = 0x00000000
    _INVALID_HANDLE_VALUE  = ctypes.c_void_p(-1).value
    _GENERIC_WRITE         = 0x40000000
    _OPEN_EXISTING         = 3

    def _create_pipe_server(name: str) -> int:
        h = ctypes.windll.kernel32.CreateNamedPipeW(
            name,
            _PIPE_ACCESS_OUTBOUND,
            _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT,
            1, 1 << 16, 1 << 16, 0, None,
        )
        if h == _INVALID_HANDLE_VALUE:
            raise OSError(f"CreateNamedPipe failed: {ctypes.GetLastError()}")
        return h

    def _write_pipe(handle: int, data: bytes) -> bool:
        written = wt.DWORD(0)
        ok = ctypes.windll.kernel32.WriteFile(
            handle, data, len(data), ctypes.byref(written), None
        )
        return bool(ok)

    # ─── Main class ───────────────────────────────────────────────────────────

    class ProcessAudioCapture:
        """
        Captures audio from a specific Windows process (by PID).

        Usage::

            cap = ProcessAudioCapture(pid=1234)
            cap.start()            # blocks until pipe is ready (≤5 s)
            # cap.ffmpeg_audio_args → list of -f/-ar/-ac/-i args for FFmpeg
            cap.stop()
        """

        PIPE_BASE = r"\\.\pipe\game_audio_"

        def __init__(self, pid: int) -> None:
            self._pid = pid
            self.pipe_name = f"{self.PIPE_BASE}{pid}"
            self._stop_event = threading.Event()
            self._pipe_ready = threading.Event()
            self._thread: threading.Thread | None = None
            self._init_error: str | None = None

        # ffmpeg args to consume this pipe as audio input
        @property
        def ffmpeg_audio_args(self) -> list[str]:
            return ["-f", "f32le", "-ar", "48000", "-ac", "2", "-i", self.pipe_name]

        def start(self) -> None:
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name=f"ProcAudio-{self._pid}",
            )
            self._thread.start()
            if not self._pipe_ready.wait(timeout=6.0):
                raise RuntimeError("ProcessAudioCapture: timed out setting up named pipe")
            if self._init_error:
                raise RuntimeError(f"ProcessAudioCapture: {self._init_error}")

        def stop(self) -> None:
            self._stop_event.set()
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=3.0)

        # ── Internal ──────────────────────────────────────────────────────────

        def _run(self) -> None:
            # ActivateAudioInterfaceAsync MUST be called from an MTA thread.
            # CoInitialize() creates STA → E_ILLEGAL_METHOD_CALL (0x8000000E).
            # CoInitializeEx(NULL, COINIT_MULTITHREADED=0) → MTA.
            _COINIT_MULTITHREADED = 0
            coinit_hr = ctypes.windll.ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
            # 0x00000000 = S_OK (MTA), 0x00000001 = S_FALSE (already MTA),
            # 0x80010106 = RPC_E_CHANGED_MODE (thread already STA — shouldn't happen)
            try:
                self._capture()
            except Exception as exc:
                if not self._pipe_ready.is_set():
                    self._init_error = (
                        f"{exc}  [coinit_hr=0x{coinit_hr & 0xFFFFFFFF:08X}]"
                    )
                    self._pipe_ready.set()
            finally:
                if coinit_hr >= 0:
                    ctypes.windll.ole32.CoUninitialize()

        def _capture(self) -> None:
            # 1. Activate IAudioClient for this process via Process Loopback API
            params = _AUDIOCLIENT_ACTIVATION_PARAMS()
            params.ActivationType = 1  # AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
            params.ProcessLoopbackParams.TargetProcessId = self._pid
            params.ProcessLoopbackParams.ProcessLoopbackMode = 0  # include child processes

            propvar = _PROPVARIANT()
            propvar.vt = _VT_BLOB
            propvar.value.blob.cbSize = ctypes.sizeof(params)
            propvar.value.blob.pBlobData = ctypes.addressof(params)

            handler = _CompletionHandler()

            # Pass handler directly — argtypes[3] is CPOINTER(...), so comtypes
            # calls handler.QueryInterface() internally and passes the correct pointer.
            op_raw = ctypes.c_void_p()
            _mmdevapi_fn(
                "VAD\\Process_Loopback",
                ctypes.byref(_IID_IAudioClient),
                ctypes.byref(propvar),
                handler,
                ctypes.byref(op_raw),
            )
            # restype=HRESULT → ctypes auto-raises OSError on failure

            if not handler.wait(5.0):
                raise RuntimeError("ActivateCompleted callback timed out")
            if handler._error:
                raise RuntimeError(handler._error)

            audio_client: _IAudioClient = handler._audio_client  # type: ignore[assignment]

            # 2. Get mix format (float32, 48 kHz, stereo typically)
            fmt_ptr = ctypes.POINTER(_WAVEFORMATEX)()
            audio_client.GetMixFormat(ctypes.byref(fmt_ptr))
            fmt = fmt_ptr.contents

            sample_rate   = fmt.nSamplesPerSec
            n_channels    = fmt.nChannels
            bytes_per_frm = fmt.nBlockAlign

            # 3. Initialize in loopback shared mode
            audio_client.Initialize(
                _AUDCLNT_SHAREMODE_SHARED,
                _AUDCLNT_STREAMFLAGS_LOOPBACK,
                0,  # use device default buffer
                0,
                fmt_ptr,
                None,
            )

            # 4. Get IAudioCaptureClient
            iid_acc = _IID_IAudioCaptureClient
            raw_cap = ctypes.c_void_p()
            audio_client.GetService(ctypes.byref(iid_acc), ctypes.byref(raw_cap))
            cap_client = ctypes.cast(
                raw_cap,
                ctypes.POINTER(_IAudioCaptureClient),
            ).contents

            # 5. Create named pipe server and wait for FFmpeg to connect
            pipe_h = _create_pipe_server(self.pipe_name)
            try:
                self._pipe_ready.set()  # unblock start()

                # ConnectNamedPipe — blocks until FFmpeg opens the pipe
                ctypes.windll.kernel32.ConnectNamedPipe(pipe_h, None)

                # 6. Start stream
                audio_client.Start()

                # 7. Read-loop: pull packets → write to pipe
                pkt_size = wt.UINT(0)
                while not self._stop_event.is_set():
                    cap_client.GetNextPacketSize(ctypes.byref(pkt_size))
                    if pkt_size.value == 0:
                        time.sleep(0.005)
                        continue

                    data_ptr   = ctypes.POINTER(ctypes.c_byte)()
                    frames_read = wt.UINT(0)
                    flags       = wt.DWORD(0)
                    dev_pos     = ctypes.c_uint64(0)
                    qpc_pos     = ctypes.c_uint64(0)

                    cap_client.GetBuffer(
                        ctypes.byref(data_ptr),
                        ctypes.byref(frames_read),
                        ctypes.byref(flags),
                        ctypes.byref(dev_pos),
                        ctypes.byref(qpc_pos),
                    )

                    n = frames_read.value
                    if n > 0:
                        raw = bytes(data_ptr[:n * bytes_per_frm])
                        ok = _write_pipe(pipe_h, raw)
                        if not ok:
                            break  # FFmpeg disconnected

                    cap_client.ReleaseBuffer(frames_read)

                audio_client.Stop()
            finally:
                ctypes.windll.kernel32.CloseHandle(pipe_h)
