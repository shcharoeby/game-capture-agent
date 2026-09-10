"""Screen event detector — pixel, template matching, OCR."""
import threading
import time
from pathlib import Path
from typing import Callable

import mss
import numpy as np
import yaml

try:
    import cv2  # type: ignore
    _OPENCV_AVAILABLE = True
except ImportError:
    _OPENCV_AVAILABLE = False

try:
    import pytesseract  # type: ignore
    _TESSERACT_AVAILABLE = True
except ImportError:
    _TESSERACT_AVAILABLE = False


DetectorCallback = Callable[[str, dict], None]  # (event_id, extra_data)


class _DetectorBase:
    def __init__(self, cfg: dict, base_dir: Path, ref_res: tuple[int, int] = (1280, 720)):
        self.id: str = cfg["id"]
        self.region: list[int] = cfg["region"]  # [x, y, w, h] in ref_res space
        self.cooldown_sec: float = float(cfg.get("cooldown_sec", 5))
        self._last_trigger: float = 0.0
        self._ref_res = ref_res
        self._base_dir = base_dir

    def _scale_region(self, win_w: int, win_h: int) -> tuple[int, int, int, int]:
        """Scale region from reference resolution to actual window size."""
        rx, ry, rw, rh = self.region
        sx = win_w / self._ref_res[0]
        sy = win_h / self._ref_res[1]
        return int(rx * sx), int(ry * sy), int(rw * sx), int(rh * sy)

    def _cooldown_ok(self) -> bool:
        return time.monotonic() - self._last_trigger >= self.cooldown_sec

    def check(self, img: np.ndarray, win_w: int, win_h: int) -> dict | None:
        """
        Check if the detector fires.
        Returns extra data dict if triggered, None otherwise.
        """
        raise NotImplementedError


class PixelDetector(_DetectorBase):
    def __init__(self, cfg: dict, base_dir: Path):
        super().__init__(cfg, base_dir)
        self._color = np.array(cfg["color"], dtype=np.uint8)
        self._tolerance: int = int(cfg.get("tolerance", 15))

    def check(self, img: np.ndarray, win_w: int, win_h: int) -> dict | None:
        if not self._cooldown_ok():
            return None
        x, y, w, h = self._scale_region(win_w, win_h)
        region_img = img[y:y + h, x:x + w]
        if region_img.size == 0:
            return None
        # Mean color of region in BGR (mss gives BGR)
        mean = region_img.mean(axis=(0, 1)).astype(np.uint8)
        # Compare with target (stored as RGB, convert)
        target_bgr = self._color[::-1]
        diff = np.abs(mean.astype(int) - target_bgr.astype(int)).max()
        if diff <= self._tolerance:
            self._last_trigger = time.monotonic()
            return {"mean_color": mean.tolist(), "diff": int(diff)}
        return None


class TemplateDetector(_DetectorBase):
    def __init__(self, cfg: dict, base_dir: Path):
        super().__init__(cfg, base_dir)
        self._threshold: float = float(cfg.get("threshold", 0.85))
        template_path = base_dir / cfg["template"]
        if _OPENCV_AVAILABLE and template_path.exists():
            self._template = cv2.imread(str(template_path))
        else:
            self._template = None

    def check(self, img: np.ndarray, win_w: int, win_h: int) -> dict | None:
        if not _OPENCV_AVAILABLE or self._template is None:
            return None
        if not self._cooldown_ok():
            return None
        x, y, w, h = self._scale_region(win_w, win_h)
        region_img = img[y:y + h, x:x + w]
        if region_img.size == 0:
            return None
        # Resize template to region if needed
        tpl = self._template
        if tpl.shape[0] > h or tpl.shape[1] > w:
            tpl = cv2.resize(tpl, (w, h))
        result = cv2.matchTemplate(region_img, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val >= self._threshold:
            self._last_trigger = time.monotonic()
            return {"confidence": float(max_val), "location": list(max_loc)}
        return None


class OCRDetector(_DetectorBase):
    def __init__(self, cfg: dict, base_dir: Path):
        super().__init__(cfg, base_dir)
        self._last_text: str = ""

    def check(self, img: np.ndarray, win_w: int, win_h: int) -> dict | None:
        if not _TESSERACT_AVAILABLE:
            return None
        if not self._cooldown_ok():
            return None
        x, y, w, h = self._scale_region(win_w, win_h)
        region_img = img[y:y + h, x:x + w]
        if region_img.size == 0:
            return None
        from PIL import Image as PILImage
        pil_img = PILImage.fromarray(region_img[:, :, ::-1])  # BGR->RGB
        text = pytesseract.image_to_string(pil_img, config="--psm 7").strip()
        if text and text != self._last_text:
            self._last_text = text
            self._last_trigger = time.monotonic()
            return {"text": text}
        return None


def _load_detectors(config_path: str | Path, base_dir: Path) -> list[_DetectorBase]:
    path = Path(config_path)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    detectors = []
    for cfg in data.get("detectors", []):
        method = cfg.get("method", "pixel")
        try:
            if method == "pixel":
                detectors.append(PixelDetector(cfg, base_dir))
            elif method == "template":
                detectors.append(TemplateDetector(cfg, base_dir))
            elif method == "ocr":
                detectors.append(OCRDetector(cfg, base_dir))
        except Exception as e:
            print(f"[detector] Failed to load detector {cfg.get('id')}: {e}")
    return detectors


class ScreenDetector:
    """
    Polls the game window for events using configured detectors.

    Requires a window handle or bbox to know where to capture.
    on_event(event_id, extra_data) is called when a detector fires.
    """

    def __init__(
        self,
        detectors_config: str | Path,
        base_dir: str | Path,
        on_event: DetectorCallback | None = None,
        poll_interval: float = 0.5,
    ):
        self._config_path = detectors_config
        self._base_dir = Path(base_dir)
        self._on_event = on_event
        self._poll_interval = poll_interval
        self._detectors: list[_DetectorBase] = []
        self._running = False
        self._thread: threading.Thread | None = None
        # Window bounding box (left, top, width, height) — set via set_window_bbox
        self._bbox: dict | None = None

    def reload_detectors(self) -> None:
        self._detectors = _load_detectors(self._config_path, self._base_dir)

    def set_window_bbox(self, left: int, top: int, width: int, height: int) -> None:
        self._bbox = {"left": left, "top": top, "width": width, "height": height}

    def start(self) -> None:
        if self._running:
            return
        self.reload_detectors()
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="ScreenDetector")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        with mss.mss() as sct:
            while self._running:
                if self._bbox and self._detectors:
                    try:
                        screenshot = sct.grab(self._bbox)
                        img = np.array(screenshot)  # BGRA
                        img = img[:, :, :3]          # drop alpha
                        w, h = self._bbox["width"], self._bbox["height"]
                        for det in self._detectors:
                            data = det.check(img, w, h)
                            if data is not None and self._on_event:
                                self._on_event(det.id, data)
                    except Exception as e:
                        print(f"[detector] Error during capture: {e}")
                time.sleep(self._poll_interval)
