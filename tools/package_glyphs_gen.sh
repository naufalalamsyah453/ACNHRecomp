#!/bin/bash
# glyphs-gen: tools/make_button_glyphs.py as one executable, put beside the player in a package. A game folder that
# ACNHRecomp-Importer prepared has no fonts/ (the importer knows nothing of this fork), so on the first start the
# player runs glyphs-gen on the folder's own RomFS to make the keyboard icons (runtime/src/glyphs_keys.cpp). It
# embeds no game data: it reads the game's font where the game is.
#
# Built on the oldest system the package runs on (CI: Ubuntu 22.04, glibc 2.35): the frozen Python must run there.
# usage: tools/package_glyphs_gen.sh [<output file>]      default: private/release/glyphs-gen
# needs: python3, python3-venv, python3-pip (PyInstaller, fontTools and zstandard are fetched into private/)
cd "$(dirname "$0")/.." || exit 1
set -e
OUT=${1:-private/release/glyphs-gen}
VENV=private/release/glyphs-gen-venv
WORK=private/release/glyphs-gen-build

[ -f tools/make_button_glyphs.py ] || { echo "not at the top of the repository"; exit 1; }
if [ ! -x "$VENV/bin/python" ]; then
    "${PYTHON:-python3}" -m venv "$VENV"
fi
"$VENV/bin/pip" install --disable-pip-version-check --quiet --upgrade pip
# fontTools reads and writes the game's font; zstandard its SARC archive. The tool's --preview (Pillow) is for
# developers, so it stays out of the executable.
"$VENV/bin/pip" install --disable-pip-version-check --quiet "pyinstaller<7" "fonttools<5" "zstandard<1"
rm -rf "$WORK"
# fontTools loads its table classes by name: the submodules have to be collected, not just the ones imported by name.
"$VENV/bin/python" -m PyInstaller --onefile --noconfirm --clean --log-level WARN \
    --name glyphs-gen --distpath "$WORK/dist" --workpath "$WORK/work" --specpath "$WORK" \
    --collect-submodules fontTools.ttLib --collect-submodules fontTools.pens \
    --collect-submodules fontTools.cu2qu --collect-submodules fontTools.misc \
    --exclude-module PIL --exclude-module tkinter --exclude-module numpy --exclude-module scipy \
    --exclude-module pytest --exclude-module setuptools \
    tools/make_button_glyphs.py
mkdir -p "$(dirname "$OUT")"
install -m 755 "$WORK/dist/glyphs-gen" "$OUT"
echo "== glyphs-gen: $OUT ($(du -h "$OUT" | cut -f1))"
"$OUT" --selftest  # the frozen executable runs, with fontTools and zstandard inside
