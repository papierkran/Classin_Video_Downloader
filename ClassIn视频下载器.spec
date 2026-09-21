# -*- mode: python ; coding: utf-8 -*-

# 说明：
# - 使用 PySide6 替代 tkinter 作为 GUI 框架
# - 程序运行时会通过 driver_auto.py 自动检测浏览器并下载/复制匹配 driver

a = Analysis(
    ['gui_pyside.py'],
    pathex=[],
    binaries=[],
    datas=[('image', 'image')],
    hiddenimports=[
        'selenium', 'requests', 'webdriver_manager', 'openpyxl',
        'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets',
        'PySide6.QtDBus', 'PySide6.QtNetwork',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'PyQt5', 'PyQt6'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ClassIn视频下载器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
