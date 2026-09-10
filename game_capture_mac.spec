# -*- mode: python ; coding: utf-8 -*-
# macOS build spec for game-capture

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config.yaml', '.'),
        ('detectors.yaml', '.'),
        ('templates', 'templates'),
    ],
    hiddenimports=[
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'pystray',
        'pystray._darwin',
        'psutil',
        'mss',
        'mss.darwin',
        'cv2',
        'pytesseract',
        'yaml',
        'numpy',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'pandas', 'pymem'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='game-capture',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=None,  # universal2 for fat binary, or None for native arch
)

app = BUNDLE(
    exe,
    name='Game Capture.app',
    icon=None,
    bundle_identifier='ru.oldenleague.game-capture',
    info_plist={
        'NSHighResolutionCapable': True,
        'NSMicrophoneUsageDescription': 'Запись аудио игры',
        'NSScreenCaptureUsageDescription': 'Захват экрана для стриминга',
    },
)
