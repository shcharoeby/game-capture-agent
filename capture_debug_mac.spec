# -*- mode: python ; coding: utf-8 -*-
# macOS build spec for capture-debug

block_cipher = None

a = Analysis(
    ['capture_debug.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('config.yaml', '.'),
    ],
    hiddenimports=[
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'psutil',
        'mss',
        'mss.darwin',
        'yaml',
        'numpy',
        'PIL',
        'PIL.Image',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'pandas', 'cv2', 'pytesseract', 'pymem'],
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
    name='capture-debug',
    debug=False,
    strip=False,
    upx=False,
    console=True,  # terminal window for debug output
    target_arch=None,
)
