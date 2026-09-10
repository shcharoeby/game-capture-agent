"""Entry point for the debug calibration tool (capture-debug)."""
import sys

from PyQt6.QtWidgets import QApplication

from src.config import Config
from src.debug.debug_gui import DebugMainWindow


def main() -> None:
    cfg = Config()
    process_names = cfg.game.get("process_names", ["heroes3.exe", "MMH55.exe", "HotA_launcher.exe"])

    app = QApplication(sys.argv)
    window = DebugMainWindow(process_names)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
