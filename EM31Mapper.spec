# PyInstaller spec — builds dist/EM31Mapper/EM31Mapper.exe (a self-contained folder).
#   pyinstaller --noconfirm EM31Mapper.spec
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

# imagecodecs ships ~60 codecs; GeoTIFF files only use a handful. Keep those.
KEEP_CODECS = {"_imcd", "_shared", "_tiff", "_zlib", "_zlibng", "_deflate", "_lzma", "_bz2",
               "_zstd", "_lz4", "_lz4f", "_jpeg8", "_jpeg2k", "_jpegxr", "_jpegls", "_ljpeg",
               "_jpegsof3", "_lerc", "_webp", "_png", "_packbits", "_lzw"}
codec_mods = [m for m in collect_submodules("imagecodecs")
              if m.count(".") == 0 or m.split(".")[1] in KEEP_CODECS or not m.split(".")[1].startswith("_")]
drop_codecs = [m for m in collect_submodules("imagecodecs") if m not in codec_mods]

a = Analysis(
    ["run_em31mapper.py"],
    pathex=[],
    datas=collect_data_files("pyqtgraph") + [("em31mapper/em31mapper.ico", "em31mapper")],
    hiddenimports=collect_submodules("pyqtgraph", filter=lambda m: ".opengl" not in m and "examples" not in m)
                  + codec_mods + ["serial.tools.list_ports_windows"],
    excludes=["tkinter", "scipy", "matplotlib", "pytest", "IPython", "pyqtgraph.opengl",
              "pyqtgraph.examples",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf", "PySide6.QtDesigner"] + drop_codecs,
    noarchive=False,
)
# software-OpenGL fallback (20 MB) and Qt Quick/QML DLLs are unused by this app
DROP = ("opengl32sw.dll", "Qt6Quick", "Qt6Qml", "Qt6Pdf", "Qt6VirtualKeyboard", "Qt6Designer")
a.binaries = [b for b in a.binaries if not any(x.lower() in b[0].lower() for x in DROP)]
a.datas = [d for d in a.datas if "translations" not in d[0] or "qtbase_en" in d[0]]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="EM31Mapper", console=False,
          icon="em31mapper/em31mapper.ico", upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="EM31Mapper", upx=False)
