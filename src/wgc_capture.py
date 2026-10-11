"""Windows Graphics Capture (WGC) — window capture via DWM compositor.

Captures the game window directly from the DWM compositor using the
Windows.Graphics.Capture API (Win10 1903+, build 18362+).

Advantages over PrintWindow/BitBlt:
- Works with DX12 flip-model swap chains (HoMM Olden Era, etc.)
- No dependency on z-order or window occlusion
- Captures the window's own GPU framebuffer

Implementation: pure ctypes + comtypes (already in requirements.txt).
No new dependencies — no winsdk/winrt package.

Frames written as raw BGRx (32-bit, top-down) to a Windows named pipe
that FFmpeg reads with: -f rawvideo -pix_fmt bgr0 -s WxH -r FPS -i \\.\pipe\game_video_PID
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time
from typing import Callable

import comtypes
import comtypes.client
import numpy as np
from PIL import Image, ImageDraw, ImageFont


# ── Windows build guard ────────────────────────────────────────────────────────

def is_wgc_supported() -> bool:
    """Return True if Windows Graphics Capture API is available (Win10 1903+)."""
    try:
        return sys.getwindowsversion().build >= 18362
    except AttributeError:
        return False  # not Windows


# ── Constants ──────────────────────────────────────────────────────────────────

_INVALID_HANDLE_VALUE   = ctypes.c_void_p(-1).value
_RO_INIT_MULTITHREADED  = 1
_DXGI_FORMAT_B8G8R8A8_UNORM = 87          # BGRA8
_D3D11_USAGE_STAGING    = 3
_D3D11_CPU_ACCESS_READ  = 0x20000
_D3D11_MAP_READ         = 1
_D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
_OVERLAY_TEXT           = "Игрок вне окна"


# ── COM GUIDs ──────────────────────────────────────────────────────────────────

def _guid(s: str) -> comtypes.GUID:
    return comtypes.GUID(s)


IID_ID3D11Device       = _guid("{DB6F6DDB-AC77-4E88-8253-819DF9BBF140}")
IID_IDXGIDevice        = _guid("{54EC77FA-1377-44E6-8C32-88FD5F44C84C}")
IID_IDirect3DDevice    = _guid("{A37624AB-8D5F-4650-9D3E-9EAE3D9BC670}")
IID_IInspectable       = _guid("{AF86E2E0-B12D-4C6A-9C5A-D7AA65101E90}")

# WinRT activation factory GUIDs
CLSID_GraphicsCaptureItem     = "Windows.Graphics.Capture.GraphicsCaptureItem"
CLSID_Direct3D11CaptureFramePool = "Windows.Graphics.Capture.Direct3D11CaptureFramePool"


# ── D3D11 structures ───────────────────────────────────────────────────────────

class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [
        ("Width",           ctypes.c_uint),
        ("Height",          ctypes.c_uint),
        ("MipLevels",       ctypes.c_uint),
        ("ArraySize",       ctypes.c_uint),
        ("Format",          ctypes.c_uint),   # DXGI_FORMAT
        ("SampleDescCount", ctypes.c_uint),
        ("SampleDescQuality", ctypes.c_uint),
        ("Usage",           ctypes.c_uint),   # D3D11_USAGE
        ("BindFlags",       ctypes.c_uint),
        ("CPUAccessFlags",  ctypes.c_uint),
        ("MiscFlags",       ctypes.c_uint),
    ]


class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [
        ("pData",      ctypes.c_void_p),
        ("RowPitch",   ctypes.c_uint),
        ("DepthPitch", ctypes.c_uint),
    ]


# ── COM interface helpers ──────────────────────────────────────────────────────

class _IUnknown(comtypes.IUnknown):
    """Base for all our COM interfaces."""
    _iid_ = comtypes.IUnknown._iid_


def _qi(obj, iid_class):
    """QueryInterface helper."""
    return obj.QueryInterface(iid_class)


# ── ID3D11Device (partial — only the slots we need) ───────────────────────────
# Vtable order: IUnknown (0-2), ID3D11Device starts at 3
# Slot 5 = CreateTexture2D (after QueryInterface, AddRef, Release = 0,1,2,
#   then CreateBuffer=3, CreateTexture1D=4, CreateTexture2D=5)
# Slot 40 = GetImmediateContext

class ID3D11Device(comtypes.IUnknown):
    _iid_ = IID_ID3D11Device
    _methods_ = [
        # slot 3: CreateBuffer
        comtypes.STDMETHOD(ctypes.HRESULT, "CreateBuffer",
            [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]),
        # slot 4: CreateTexture1D
        comtypes.STDMETHOD(ctypes.HRESULT, "CreateTexture1D",
            [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]),
        # slot 5: CreateTexture2D
        comtypes.STDMETHOD(ctypes.HRESULT, "CreateTexture2D",
            [ctypes.POINTER(D3D11_TEXTURE2D_DESC),
             ctypes.c_void_p,
             ctypes.POINTER(ctypes.c_void_p)]),
        # slots 6-39: stubs (36 methods)
        *[comtypes.STDMETHOD(ctypes.HRESULT, f"_stub{i}", []) for i in range(6, 40)],
        # slot 40: GetImmediateContext
        comtypes.STDMETHOD(None, "GetImmediateContext",
            [ctypes.POINTER(ctypes.c_void_p)]),
    ]


# ── ID3D11DeviceContext (partial) ──────────────────────────────────────────────
# slot 14 = Map, slot 15 = Unmap, slot 47 = CopyResource
# IUnknown = 0-2, ID3D11DeviceChild = 3 (GetDevice), 4 (GetPrivateData),
#   5 (SetPrivateData), 6 (SetPrivateDataInterface)
# Then ID3D11DeviceContext starts at slot 7:
# VSSetConstantBuffers=7, PSSetShaderResources=8, PSSetShader=9, PSSetSamplers=10,
# VSSetShader=11, DrawIndexed=12, Draw=13, Map=14, Unmap=15, ...

class ID3D11DeviceContext(comtypes.IUnknown):
    _iid_ = _guid("{C0BFA96C-E089-44FB-8EAF-26F8796190DA}")
    _methods_ = [
        # slots 3-6: ID3D11DeviceChild stubs
        *[comtypes.STDMETHOD(ctypes.HRESULT, f"_dc_stub{i}", []) for i in range(3, 7)],
        # slots 7-13: draw stubs
        *[comtypes.STDMETHOD(ctypes.HRESULT, f"_dc_draw{i}", []) for i in range(7, 14)],
        # slot 14: Map
        comtypes.STDMETHOD(ctypes.HRESULT, "Map",
            [ctypes.c_void_p,            # pResource
             ctypes.c_uint,              # Subresource
             ctypes.c_uint,              # MapType
             ctypes.c_uint,              # MapFlags
             ctypes.POINTER(D3D11_MAPPED_SUBRESOURCE)]),
        # slot 15: Unmap
        comtypes.STDMETHOD(None, "Unmap",
            [ctypes.c_void_p,            # pResource
             ctypes.c_uint]),            # Subresource
        # slots 16-46: stubs
        *[comtypes.STDMETHOD(ctypes.HRESULT, f"_dc_misc{i}", []) for i in range(16, 47)],
        # slot 47: CopyResource
        comtypes.STDMETHOD(None, "CopyResource",
            [ctypes.c_void_p,            # pDstResource
             ctypes.c_void_p]),          # pSrcResource
    ]


# ── IDXGIDevice ────────────────────────────────────────────────────────────────

class IDXGIDevice(comtypes.IUnknown):
    _iid_ = IID_IDXGIDevice
    _methods_ = [
        # IDXGIObject: SetPrivateData, SetPrivateDataInterface, GetPrivateData, GetParent
        *[comtypes.STDMETHOD(ctypes.HRESULT, f"_dxgi_obj{i}", []) for i in range(3, 7)],
        # IDXGIDevice: GetAdapter, CreateSurface, QueryResourceResidency, SetGPUThreadPriority, GetGPUThreadPriority
        *[comtypes.STDMETHOD(ctypes.HRESULT, f"_dxgi_dev{i}", []) for i in range(7, 12)],
    ]


# ── WinRT IInspectable ─────────────────────────────────────────────────────────

class IInspectable(comtypes.IUnknown):
    _iid_ = IID_IInspectable
    _methods_ = [
        comtypes.STDMETHOD(ctypes.HRESULT, "GetIids",
            [ctypes.c_void_p, ctypes.c_void_p]),
        comtypes.STDMETHOD(ctypes.HRESULT, "GetRuntimeClassName",
            [ctypes.c_void_p]),
        comtypes.STDMETHOD(ctypes.HRESULT, "GetTrustLevel",
            [ctypes.c_void_p]),
    ]


# ── IGraphicsCaptureItemInterop ────────────────────────────────────────────────
# IID: 3628E81B-3CAC-4C60-B7F4-23CE0E0C3356
# Methods: IUnknown (0-2) + CreateForWindow (3) + CreateForMonitor (4)

class IGraphicsCaptureItemInterop(comtypes.IUnknown):
    _iid_ = _guid("{3628E81B-3CAC-4C60-B7F4-23CE0E0C3356}")
    _methods_ = [
        # slot 3: CreateForWindow(HWND, REFIID, void**)
        comtypes.STDMETHOD(ctypes.HRESULT, "CreateForWindow",
            [wt.HWND,
             ctypes.POINTER(comtypes.GUID),
             ctypes.POINTER(ctypes.c_void_p)]),
        # slot 4: CreateForMonitor(HMONITOR, REFIID, void**)
        comtypes.STDMETHOD(ctypes.HRESULT, "CreateForMonitor",
            [ctypes.c_void_p,
             ctypes.POINTER(comtypes.GUID),
             ctypes.POINTER(ctypes.c_void_p)]),
    ]


# ── IGraphicsCaptureItem ───────────────────────────────────────────────────────
# IID: 79C3F95B-31F7-4EC2-A464-632EF5D30760

class IGraphicsCaptureItem(IInspectable):
    _iid_ = _guid("{79C3F95B-31F7-4EC2-A464-632EF5D30760}")
    _methods_ = [
        # DisplayName (HSTRING*)
        comtypes.STDMETHOD(ctypes.HRESULT, "get_DisplayName",
            [ctypes.c_void_p]),
        # Size (SizeInt32*)
        comtypes.STDMETHOD(ctypes.HRESULT, "get_Size",
            [ctypes.c_void_p]),
        # add_Closed / remove_Closed
        comtypes.STDMETHOD(ctypes.HRESULT, "add_Closed",
            [ctypes.c_void_p, ctypes.c_void_p]),
        comtypes.STDMETHOD(ctypes.HRESULT, "remove_Closed",
            [ctypes.c_void_p]),
    ]


# ── IDirect3D11CaptureFramePool ────────────────────────────────────────────────
# IID: 24EB6D22-1975-422E-82E7-780DBD8DDF24

class IDirect3D11CaptureFramePool(IInspectable):
    _iid_ = _guid("{24EB6D22-1975-422E-82E7-780DBD8DDF24}")
    _methods_ = [
        # Recreate
        comtypes.STDMETHOD(ctypes.HRESULT, "Recreate",
            [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_void_p]),
        # TryGetNextFrame() -> IDirect3D11CaptureFrame*
        comtypes.STDMETHOD(ctypes.HRESULT, "TryGetNextFrame",
            [ctypes.POINTER(ctypes.c_void_p)]),
        # add_FrameArrived
        comtypes.STDMETHOD(ctypes.HRESULT, "add_FrameArrived",
            [ctypes.c_void_p, ctypes.c_void_p]),
        # remove_FrameArrived
        comtypes.STDMETHOD(ctypes.HRESULT, "remove_FrameArrived",
            [ctypes.c_void_p]),
        # CreateCaptureSession(item) -> IGraphicsCaptureSession*
        comtypes.STDMETHOD(ctypes.HRESULT, "CreateCaptureSession",
            [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]),
    ]


# ── IDirect3D11CaptureFramePoolStatics ────────────────────────────────────────
# IID: 7784056A-67AA-4D53-AE54-1088D5A8CA21

class IDirect3D11CaptureFramePoolStatics(IInspectable):
    _iid_ = _guid("{7784056A-67AA-4D53-AE54-1088D5A8CA21}")
    _methods_ = [
        # Create(device, pixelFormat, numberOfBuffers, size) -> IDirect3D11CaptureFramePool*
        comtypes.STDMETHOD(ctypes.HRESULT, "Create",
            [ctypes.c_void_p,           # IDirect3DDevice*
             ctypes.c_uint,             # DirectXPixelFormat
             ctypes.c_int,              # numberOfBuffers
             ctypes.c_int64,            # SizeInt32 (packed as int64: width | height<<32)
             ctypes.POINTER(ctypes.c_void_p)]),
    ]


# ── IDirect3D11CaptureFrame ────────────────────────────────────────────────────
# IID: FA50C623-38DA-4B32-ACF3-FA9734AD800E

class IDirect3D11CaptureFrame(IInspectable):
    _iid_ = _guid("{FA50C623-38DA-4B32-ACF3-FA9734AD800E}")
    _methods_ = [
        # Surface -> IDirect3DSurface
        comtypes.STDMETHOD(ctypes.HRESULT, "get_Surface",
            [ctypes.POINTER(ctypes.c_void_p)]),
        # SystemRelativeTime
        comtypes.STDMETHOD(ctypes.HRESULT, "get_SystemRelativeTime",
            [ctypes.c_void_p]),
        # ContentSize
        comtypes.STDMETHOD(ctypes.HRESULT, "get_ContentSize",
            [ctypes.c_void_p]),
    ]


# ── IGraphicsCaptureSession ────────────────────────────────────────────────────
# IID: 814E42A9-F70F-4AD7-939B-FDDCC6EB880D

class IGraphicsCaptureSession(IInspectable):
    _iid_ = _guid("{814E42A9-F70F-4AD7-939B-FDDCC6EB880D}")
    _methods_ = [
        # IsBorderRequired (get/put)
        comtypes.STDMETHOD(ctypes.HRESULT, "get_IsBorderRequired",
            [ctypes.POINTER(ctypes.c_bool)]),
        comtypes.STDMETHOD(ctypes.HRESULT, "put_IsBorderRequired",
            [ctypes.c_bool]),
        # StartCapture
        comtypes.STDMETHOD(ctypes.HRESULT, "StartCapture", []),
        # Close
        comtypes.STDMETHOD(ctypes.HRESULT, "Close", []),
    ]


# ── IGraphicsCaptureSession2 ───────────────────────────────────────────────────
# IID: 2C4D2F7A-95FD-4BDC-9C08-6BCBA5E39C0F
# Needed for IsCursorCaptureEnabled = False (no cursor in stream)

class IGraphicsCaptureSession2(IInspectable):
    _iid_ = _guid("{2C4D2F7A-95FD-4BDC-9C08-6BCBA5E39C0F}")
    _methods_ = [
        comtypes.STDMETHOD(ctypes.HRESULT, "get_IsCursorCaptureEnabled",
            [ctypes.POINTER(ctypes.c_bool)]),
        comtypes.STDMETHOD(ctypes.HRESULT, "put_IsCursorCaptureEnabled",
            [ctypes.c_bool]),
    ]


# ── IDirect3DDxgiInterfaceAccess ───────────────────────────────────────────────
# Used to get the underlying ID3D11Texture2D from an IDirect3DSurface
# IID: A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1

class IDirect3DDxgiInterfaceAccess(comtypes.IUnknown):
    _iid_ = _guid("{A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1}")
    _methods_ = [
        # GetInterface(REFIID, void**)
        comtypes.STDMETHOD(ctypes.HRESULT, "GetInterface",
            [ctypes.POINTER(comtypes.GUID),
             ctypes.POINTER(ctypes.c_void_p)]),
    ]


IID_ID3D11Texture2D = _guid("{6F15AAF2-D208-4E89-9AB4-489535D34F9C}")
IID_ID3D11Resource  = _guid("{DC8E63F3-D12B-4952-B47B-5E45026A862D}")


# ── WinRT helpers ──────────────────────────────────────────────────────────────

_combase = ctypes.WinDLL("combase.dll")
_d3d11   = ctypes.WinDLL("d3d11.dll")

_combase.RoInitialize.restype     = ctypes.HRESULT
_combase.RoInitialize.argtypes    = [ctypes.c_uint]

_combase.RoGetActivationFactory.restype  = ctypes.HRESULT
_combase.RoGetActivationFactory.argtypes = [
    ctypes.c_void_p,              # HSTRING activatableClassId
    ctypes.POINTER(comtypes.GUID),# REFIID iid
    ctypes.POINTER(ctypes.c_void_p),  # factory**
]

_combase.WindowsCreateString.restype  = ctypes.HRESULT
_combase.WindowsCreateString.argtypes = [
    ctypes.c_wchar_p,  # sourceString
    ctypes.c_uint,     # length
    ctypes.c_void_p,   # HSTRING*
]

_combase.WindowsDeleteString.restype  = ctypes.HRESULT
_combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]  # HSTRING


def _make_hstring(s: str) -> ctypes.c_void_p:
    hs = ctypes.c_void_p()
    hr = _combase.WindowsCreateString(s, len(s), ctypes.byref(hs))
    if hr < 0:
        raise OSError(f"WindowsCreateString failed: 0x{hr & 0xFFFFFFFF:08X}")
    return hs


def _ro_get_factory(class_id: str, iid_class) -> ctypes.c_void_p:
    hs = _make_hstring(class_id)
    try:
        factory_ptr = ctypes.c_void_p()
        iid = iid_class._iid_
        hr = _combase.RoGetActivationFactory(hs, ctypes.byref(iid),
                                              ctypes.byref(factory_ptr))
        if hr < 0:
            raise OSError(f"RoGetActivationFactory({class_id!r}) failed: "
                          f"0x{hr & 0xFFFFFFFF:08X}")
        return factory_ptr
    finally:
        _combase.WindowsDeleteString(hs)


def _create_direct3d11_device_from_dxgi(dxgi_device_ptr: ctypes.c_void_p) -> ctypes.c_void_p:
    """CreateDirect3D11DeviceFromDXGIDevice → WinRT IDirect3DDevice.

    This function is exported from d3d11.dll (Win8+), NOT from the WinRT
    component windows.graphics.directx.direct3d11.dll which is not a
    traditional Win32 DLL and cannot be loaded via ctypes.WinDLL().
    """
    _d3d11.CreateDirect3D11DeviceFromDXGIDevice.restype  = ctypes.HRESULT
    _d3d11.CreateDirect3D11DeviceFromDXGIDevice.argtypes = [
        ctypes.c_void_p,                  # IDXGIDevice*
        ctypes.POINTER(ctypes.c_void_p),  # IInspectable** (IDirect3DDevice)
    ]
    result = ctypes.c_void_p()
    hr = _d3d11.CreateDirect3D11DeviceFromDXGIDevice(
        dxgi_device_ptr, ctypes.byref(result)
    )
    if hr < 0:
        raise OSError(f"CreateDirect3D11DeviceFromDXGIDevice failed: "
                      f"0x{hr & 0xFFFFFFFF:08X}")
    return result


# ── Overlay drawing (PIL) ──────────────────────────────────────────────────────

_OVERLAY_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}

def _get_overlay_font(height: int):
    font_size = max(28, height // 20)
    if font_size not in _OVERLAY_FONT_CACHE:
        try:
            _OVERLAY_FONT_CACHE[font_size] = ImageFont.truetype("arial.ttf", font_size)
        except (OSError, IOError):
            try:
                import os
                winfonts = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
                _OVERLAY_FONT_CACHE[font_size] = ImageFont.truetype(
                    os.path.join(winfonts, "arial.ttf"), font_size
                )
            except (OSError, IOError):
                _OVERLAY_FONT_CACHE[font_size] = ImageFont.load_default()
    return _OVERLAY_FONT_CACHE[font_size]


def _draw_focus_overlay(buf: bytearray, w: int, h: int) -> None:
    """Draw black background + centred overlay text into a BGRA bytearray in-place."""
    img = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1)
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (w, h)], fill=(0, 0, 0, 255))

    font = _get_overlay_font(h)
    bbox = draw.textbbox((0, 0), _OVERLAY_TEXT, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = max(0, (w - tw) // 2)
    ty = max(0, (h - th) // 2)
    draw.text((tx, ty), _OVERLAY_TEXT, fill=(255, 255, 255, 255), font=font)

    raw = img.tobytes("raw", "BGRA")
    buf[:] = raw


# ── _find_hwnd (copied from window_capture.py) ────────────────────────────────

def _find_hwnd(pid: int) -> int:
    found = wt.HWND(0)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        p = wt.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            found.value = hwnd
            return False
        return True

    ctypes.windll.user32.EnumWindows(_cb, 0)
    return found.value


# ── WgcCapturePipe ─────────────────────────────────────────────────────────────

class WgcCapturePipe:
    """
    Captures a game window using Windows Graphics Capture API (Win10 1903+).

    Reads the GPU framebuffer via DWM compositor — works for DX12 flip-model
    swap chains (HoMM Olden Era, etc.) where PrintWindow/BitBlt return black.

    Interface identical to WindowCapturePipe:
      - Constructor: (pid, fps, on_focus_change)
      - Properties:  pipe_name, width, height
      - Methods:     start() / stop()
    """

    PIPE_BASE = r"\\.\pipe\game_video_"

    def __init__(
        self,
        pid: int,
        fps: int = 60,
        on_focus_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._pid = pid
        self._fps = fps
        self._on_focus_change = on_focus_change
        self.pipe_name = f"{self.PIPE_BASE}{pid}"
        self.width  = 0
        self.height = 0
        self._stop_event  = threading.Event()
        self._pipe_ready  = threading.Event()
        self._thread: threading.Thread | None = None
        self._init_error: str | None = None

    def start(self) -> None:
        """Start capture thread; blocks until the pipe is ready (≤5 s)."""
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"WgcCapture-{self._pid}"
        )
        self._thread.start()
        if not self._pipe_ready.wait(timeout=5.0):
            raise RuntimeError("WgcCapturePipe: timed out initialising WGC capture")
        if self._init_error:
            raise RuntimeError(f"WgcCapturePipe: {self._init_error}")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    # ── internals ─────────────────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            self._capture()
        except Exception as exc:
            if not self._pipe_ready.is_set():
                self._init_error = str(exc)
                self._pipe_ready.set()

    def _capture(self) -> None:  # noqa: C901 (complex but linear init sequence)
        # 1. WinRT initialisation
        hr = _combase.RoInitialize(_RO_INIT_MULTITHREADED)
        if hr < 0 and hr != -2147417850:  # 0x80010106 = already initialised
            raise OSError(f"RoInitialize failed: 0x{hr & 0xFFFFFFFF:08X}")

        # 2. Create D3D11 device
        _d3d11.D3D11CreateDevice.restype  = ctypes.HRESULT
        _d3d11.D3D11CreateDevice.argtypes = [
            ctypes.c_void_p,  # pAdapter
            ctypes.c_uint,    # DriverType (D3D_DRIVER_TYPE_HARDWARE=1)
            ctypes.c_void_p,  # Software
            ctypes.c_uint,    # Flags
            ctypes.c_void_p,  # pFeatureLevels
            ctypes.c_uint,    # FeatureLevels
            ctypes.c_uint,    # SDKVersion (D3D11_SDK_VERSION=7)
            ctypes.POINTER(ctypes.c_void_p),  # ppDevice
            ctypes.c_void_p,  # pFeatureLevel
            ctypes.POINTER(ctypes.c_void_p),  # ppImmediateContext
        ]
        d3d_device_ptr  = ctypes.c_void_p()
        d3d_context_ptr = ctypes.c_void_p()
        hr = _d3d11.D3D11CreateDevice(
            None, 1, None,                     # hardware adapter
            _D3D11_CREATE_DEVICE_BGRA_SUPPORT, # flags
            None, 0, 7,                        # feature levels (any), SDK version
            ctypes.byref(d3d_device_ptr),
            None,
            ctypes.byref(d3d_context_ptr),
        )
        if hr < 0:
            raise OSError(f"D3D11CreateDevice failed: 0x{hr & 0xFFFFFFFF:08X}")

        d3d_device  = ctypes.cast(d3d_device_ptr,  ctypes.POINTER(ID3D11Device))
        d3d_context = ctypes.cast(d3d_context_ptr, ctypes.POINTER(ID3D11DeviceContext))

        # 3. ID3D11Device → IDXGIDevice → WinRT IDirect3DDevice
        # NOTE: ctypes.POINTER(COMInterface) is intercepted by comtypes and returns
        # a comtypes LP_* type whose instances do NOT support [0] subscripting.
        # Call methods directly on the pointer instance instead (comtypes LP_ types
        # forward attribute access to the underlying COM object automatically).
        dxgi_device = d3d_device.QueryInterface(IDXGIDevice)
        winrt_device = _create_direct3d11_device_from_dxgi(
            ctypes.cast(dxgi_device, ctypes.c_void_p)
        )

        # 4. Find window and get its size
        hwnd = _find_hwnd(self._pid)
        if not hwnd:
            raise RuntimeError(f"No visible window for PID {self._pid}")

        rect = wt.RECT()
        ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect))
        w, h = rect.right, rect.bottom
        if w <= 0 or h <= 0:
            raise RuntimeError(f"Game window has zero client area ({w}×{h})")

        self.width  = w
        self.height = h

        # 5. Create IGraphicsCaptureItem for the window
        item_factory_ptr = _ro_get_factory(CLSID_GraphicsCaptureItem,
                                            IGraphicsCaptureItemInterop)
        item_interop = ctypes.cast(item_factory_ptr,
                                    ctypes.POINTER(IGraphicsCaptureItemInterop))
        item_iid = IGraphicsCaptureItem._iid_
        item_ptr = ctypes.c_void_p()
        hr = item_interop.CreateForWindow(
            wt.HWND(hwnd),
            ctypes.byref(item_iid),
            ctypes.byref(item_ptr),
        )
        if hr < 0:
            raise OSError(f"IGraphicsCaptureItemInterop.CreateForWindow failed: "
                          f"0x{hr & 0xFFFFFFFF:08X}")

        # 6. Create frame pool
        pool_factory_ptr = _ro_get_factory(CLSID_Direct3D11CaptureFramePool,
                                            IDirect3D11CaptureFramePoolStatics)
        pool_statics = ctypes.cast(pool_factory_ptr,
                                    ctypes.POINTER(IDirect3D11CaptureFramePoolStatics))

        # SizeInt32 packed as int64: low 32 = width, high 32 = height
        size_packed = ctypes.c_int64(w | (h << 32))
        pool_ptr = ctypes.c_void_p()
        hr = pool_statics.Create(
            winrt_device,
            _DXGI_FORMAT_B8G8R8A8_UNORM,
            2,                   # number of buffers
            size_packed,
            ctypes.byref(pool_ptr),
        )
        if hr < 0:
            raise OSError(f"IDirect3D11CaptureFramePoolStatics.Create failed: "
                          f"0x{hr & 0xFFFFFFFF:08X}")

        frame_pool = ctypes.cast(pool_ptr, ctypes.POINTER(IDirect3D11CaptureFramePool))

        # 7. Create capture session
        session_ptr = ctypes.c_void_p()
        hr = frame_pool.CreateCaptureSession(item_ptr, ctypes.byref(session_ptr))
        if hr < 0:
            raise OSError(f"IDirect3D11CaptureFramePool.CreateCaptureSession failed: "
                          f"0x{hr & 0xFFFFFFFF:08X}")

        session = ctypes.cast(session_ptr, ctypes.POINTER(IGraphicsCaptureSession))

        # Try to disable cursor capture (Win10 2004+ feature — optional)
        try:
            session2 = session.QueryInterface(IGraphicsCaptureSession2)
            session2.put_IsCursorCaptureEnabled(False)
        except Exception:
            pass  # older Windows — cursor will appear in stream, acceptable

        # 8. Create CPU-readable staging texture
        staging_desc = D3D11_TEXTURE2D_DESC(
            Width=w, Height=h,
            MipLevels=1, ArraySize=1,
            Format=_DXGI_FORMAT_B8G8R8A8_UNORM,
            SampleDescCount=1, SampleDescQuality=0,
            Usage=_D3D11_USAGE_STAGING,
            BindFlags=0,
            CPUAccessFlags=_D3D11_CPU_ACCESS_READ,
            MiscFlags=0,
        )
        staging_ptr = ctypes.c_void_p()
        hr = d3d_device.CreateTexture2D(
            ctypes.byref(staging_desc),
            None,
            ctypes.byref(staging_ptr),
        )
        if hr < 0:
            raise OSError(f"ID3D11Device.CreateTexture2D (staging) failed: "
                          f"0x{hr & 0xFFFFFFFF:08X}")

        # 9. Named pipe (identical to window_capture.py)
        pipe_h = ctypes.windll.kernel32.CreateNamedPipeW(
            self.pipe_name,
            0x00000002,   # PIPE_ACCESS_OUTBOUND
            0x00000000,   # PIPE_TYPE_BYTE | PIPE_WAIT
            1, 8 << 20, 8 << 20, 0, None,
        )
        if pipe_h == _INVALID_HANDLE_VALUE:
            raise OSError(f"CreateNamedPipe failed: error {ctypes.GetLastError()}")

        # 10. Pipe is ready — unblock start()
        self._pipe_ready.set()

        try:
            # 11. Wait for FFmpeg to connect
            ctypes.windll.kernel32.ConnectNamedPipe(pipe_h, None)

            # 12. Start capture
            hr = session.StartCapture()
            if hr < 0:
                raise OSError(f"IGraphicsCaptureSession.StartCapture failed: "
                               f"0x{hr & 0xFFFFFFFF:08X}")

            # ── Frame buffer (BGRA, will be converted to BGRx in-place) ────────
            frame_buf       = bytearray(w * h * 4)
            last_frame_buf  = bytearray(w * h * 4)  # hold-last-frame
            written         = wt.DWORD(0)
            interval        = 1.0 / self._fps
            next_tick       = time.monotonic()

            # Focus tracking (same logic as window_capture.py)
            _GetForegroundWindow = ctypes.windll.user32.GetForegroundWindow
            _CALLBACK_GRACE_SEC  = 4.0
            _focused:             bool  = True
            _focus_lost_at:       float | None = None
            _focus_loss_fired:    bool  = False

            # 13. Main loop
            while not self._stop_event.is_set():
                now = time.monotonic()
                is_focused = (_GetForegroundWindow() == hwnd)

                # ── Focus tracking ─────────────────────────────────────────
                if is_focused:
                    if not _focused:
                        _focus_lost_at = None
                        if _focus_loss_fired:
                            _focus_loss_fired = False
                            if self._on_focus_change is not None:
                                try:
                                    self._on_focus_change(True)
                                except Exception:
                                    pass
                    _focused = True
                else:
                    if _focused:
                        _focus_lost_at = now
                    _focused = False
                    if (
                        not _focus_loss_fired
                        and _focus_lost_at is not None
                        and now - _focus_lost_at >= _CALLBACK_GRACE_SEC
                    ):
                        _focus_loss_fired = True
                        if self._on_focus_change is not None:
                            try:
                                self._on_focus_change(False)
                            except Exception:
                                pass

                _show_overlay = not _focused and _focus_lost_at is not None

                if _show_overlay:
                    # Draw overlay into frame_buf (no GPU readback needed)
                    _draw_focus_overlay(frame_buf, w, h)
                else:
                    # ── Try to get a new frame from WGC ────────────────────
                    raw_frame_ptr = ctypes.c_void_p()
                    hr = frame_pool.TryGetNextFrame(ctypes.byref(raw_frame_ptr))

                    if hr >= 0 and raw_frame_ptr.value:
                        raw_frame = ctypes.cast(raw_frame_ptr,
                                                ctypes.POINTER(IDirect3D11CaptureFrame))
                        # Get surface
                        surface_ptr = ctypes.c_void_p()
                        hr2 = raw_frame.get_Surface(ctypes.byref(surface_ptr))

                        if hr2 >= 0 and surface_ptr.value:
                            # IDirect3DSurface → IDirect3DDxgiInterfaceAccess
                            surf_unk = ctypes.cast(
                                surface_ptr, ctypes.POINTER(comtypes.IUnknown)
                            )
                            try:
                                dxgi_access = surf_unk.QueryInterface(
                                    IDirect3DDxgiInterfaceAccess
                                )
                                # → ID3D11Texture2D
                                tex_iid = IID_ID3D11Texture2D
                                tex_ptr = ctypes.c_void_p()
                                hr3 = dxgi_access.GetInterface(
                                    ctypes.byref(tex_iid), ctypes.byref(tex_ptr)
                                )
                                if hr3 >= 0 and tex_ptr.value:
                                    # CopyResource: GPU tex → staging tex
                                    d3d_context.CopyResource(
                                        staging_ptr,
                                        tex_ptr,
                                    )
                                    # Map staging texture for CPU read
                                    mapped = D3D11_MAPPED_SUBRESOURCE()
                                    hr4 = d3d_context.Map(
                                        staging_ptr, 0,
                                        _D3D11_MAP_READ, 0,
                                        ctypes.byref(mapped),
                                    )
                                    if hr4 >= 0:
                                        # Copy rows (RowPitch may include padding)
                                        row_pitch = mapped.RowPitch
                                        row_bytes = w * 4
                                        src = mapped.pData
                                        if row_pitch == row_bytes:
                                            # Fast path: contiguous
                                            ctypes.memmove(
                                                (ctypes.c_byte * len(frame_buf))
                                                .from_buffer(frame_buf),
                                                src, len(frame_buf)
                                            )
                                        else:
                                            # Slow path: skip row padding
                                            for row in range(h):
                                                offset_dst = row * row_bytes
                                                offset_src = row * row_pitch
                                                ctypes.memmove(
                                                    (ctypes.c_byte * row_bytes)
                                                    .from_buffer(frame_buf,
                                                                  offset_dst),
                                                    src + offset_src,
                                                    row_bytes,
                                                )
                                        d3d_context.Unmap(staging_ptr, 0)
                                        last_frame_buf[:] = frame_buf
                                    # Release ID3D11Texture2D
                                    unk_tex = ctypes.cast(
                                        tex_ptr, ctypes.POINTER(comtypes.IUnknown)
                                    )
                                    unk_tex.Release()
                            except Exception:
                                pass
                            # Release IDirect3DSurface
                            surf_unk.Release()

                        # Close frame (return slot to pool)
                        unk_frame = ctypes.cast(
                            raw_frame_ptr, ctypes.POINTER(comtypes.IUnknown)
                        )
                        unk_frame.Release()
                    else:
                        # No new frame — hold last frame
                        frame_buf[:] = last_frame_buf

                # Convert BGRA → BGRx: set alpha channel to 0
                # (FFmpeg expects bgr0: B G R 0x00)
                arr = np.frombuffer(frame_buf, dtype=np.uint8).reshape((h, w, 4))
                arr[:, :, 3] = 0
                bgr0_bytes = arr.tobytes()

                # Write frame to pipe
                ok = ctypes.windll.kernel32.WriteFile(
                    pipe_h, bgr0_bytes, len(bgr0_bytes),
                    ctypes.byref(written), None,
                )
                if not ok:
                    break  # FFmpeg disconnected

                # Pace to target FPS
                next_tick += interval
                delay = next_tick - time.monotonic()
                if delay > 0:
                    time.sleep(delay)

        finally:
            # Cleanup COM objects
            try:
                session.Close()
            except Exception:
                pass
            try:
                unk_pool = ctypes.cast(
                    pool_ptr, ctypes.POINTER(comtypes.IUnknown)
                )
                unk_pool.Release()
            except Exception:
                pass
            try:
                unk_item = ctypes.cast(
                    item_ptr, ctypes.POINTER(comtypes.IUnknown)
                )
                unk_item.Release()
            except Exception:
                pass
            try:
                unk_staging = ctypes.cast(
                    staging_ptr, ctypes.POINTER(comtypes.IUnknown)
                )
                unk_staging.Release()
            except Exception:
                pass
            try:
                d3d_context.Release()
            except Exception:
                pass
            try:
                d3d_device.Release()
            except Exception:
                pass
            ctypes.windll.kernel32.CloseHandle(pipe_h)
