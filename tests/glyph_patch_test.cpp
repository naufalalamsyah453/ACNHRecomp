// The keyboard icons' byte-level steps (runtime/src/glyphs_patch.cpp), with no game data:
//
//   glyph_patch_test [<dir>]
//       patches a synthetic archive from a manifest, checks every byte of the result (the idDelta words, the BFTTF
//       XOR) and the raw-block zstd frame around it, then writes <dir>/frame.zs and <dir>/expected.bin so a zstd
//       decoder can check the frame (the CI's verify job decompresses it). <dir> is created if missing.
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <string>

#include "glyphs_patch.h"

namespace fs = std::filesystem;

namespace {

int failures = 0;

void check(bool ok, const char* what) {
    if (!ok) {
        std::fprintf(stderr, "FAIL %s\n", what);
        failures++;
    }
}

void write(const fs::path& path, const std::string& data) {
    std::ofstream f(path, std::ios::binary);
    f.write(data.data(), std::streamsize(data.size()));
}

void put16(std::string& s, size_t at, uint16_t v) {
    s[at] = char(uint8_t(v >> 8));
    s[at + 1] = char(uint8_t(v));
}

}  // namespace

int main(int argc, char** argv) {
    const fs::path out = argc > 1 ? fs::path(argv[1]) : fs::path("glyph_patch_out");

    // A manifest like the tool's: the font starts at base 8 (the BFTTF header), one idDelta word per prompt relative
    // to it, and one block of keycap glyphs per prompt.
    const std::string text =
        "# keyboard icons\n"
        "key 1a2b3c4d\n"
        "base 8\n"
        "labels A,SPC,KEY\n"
        "cp e0e0 a 16 300\n"
        "cp e0e1 b 1000 400\n";
    nx::glyphs::Manifest m;
    check(m.load(text), "manifest loads");
    check(m.key == 0x1A2B3C4D, "key");
    check(m.base == 8, "base");
    check(m.labels.size() == 3 && m.labels[1] == "SPC", "labels");
    check(m.entries.size() == 2 && m.entries[0].cp == 0xE0E0 && m.entries[0].offset == 16, "entries");

    std::string base(300000, '\0');
    for (size_t i = 0; i < base.size(); i++) base[i] = char((i * 37 + 11) & 0xFF);

    std::map<std::string, std::string> bound = {{"a", "A"}, {"b", "SPC"}};
    std::string patched = base;
    check(nx::glyphs::patch(patched, m, bound), "patch");
    // A at label 0: gid 300; SPC at label 1: gid 401. The stored value is (gid - cp), big endian, XORed with the
    // key's byte for the word at that offset from the font's start (the archive is the BFTTF-encoded font).
    const uint8_t k[4] = {0x4D, 0x3C, 0x2B, 0x1A};
    std::string expected = base;
    put16(expected, 8 + 16, uint16_t((300 - 0xE0E0) & 0xFFFF));
    put16(expected, 8 + 1000, uint16_t((401 - 0xE0E1) & 0xFFFF));
    for (size_t off : {size_t(16), size_t(1000)}) {
        expected[8 + off] = char(uint8_t(expected[8 + off]) ^ k[off % 4]);
        expected[8 + off + 1] = char(uint8_t(expected[8 + off + 1]) ^ k[(off + 1) % 4]);
    }
    check(patched == expected, "patched bytes");

    // A key with no keycap, and an offset past the end: refused, not a half-patched archive silently served.
    std::map<std::string, std::string> unknown = {{"a", "NOPE"}, {"b", "SPC"}};
    check(!nx::glyphs::patch(patched, m, unknown), "unknown label refused");
    nx::glyphs::Manifest far = m;
    far.entries[0].offset = base.size() - 1;
    check(!nx::glyphs::patch(patched, far, bound), "offset past the end refused");

    // The frame: magic, single-segment header with the content size, then raw blocks of at most 128 KiB.
    const std::string frame = nx::glyphs::zstd_frame(patched);
    check(frame.size() == 4 + 1 + 4 + 3 * 3 + patched.size(), "frame size");  // 3 raw blocks of 128 KiB
    check(uint8_t(frame[0]) == 0x28 && uint8_t(frame[1]) == 0xB5 && uint8_t(frame[2]) == 0x2F &&
              uint8_t(frame[3]) == 0xFD,
          "frame magic");
    check((uint8_t(frame[4]) & 0x20) != 0, "single segment");
    check(uint32_t(uint8_t(frame[5])) == (uint32_t(patched.size()) & 0xFF), "content size, low byte");
    const std::string small = nx::glyphs::zstd_frame(std::string(100, 'x'));
    check(small.size() == 4 + 1 + 1 + 3 + 100, "one-byte content size frame");

    std::error_code ec;
    fs::create_directories(out, ec);
    write(out / "frame.zs", frame);
    write(out / "expected.bin", patched);
    write(out / "small.zs", small);
    std::printf("glyph_patch: %s (%zu bytes, %zu prompts)\n", failures ? "FAILED" : "ok", frame.size(),
                m.entries.size());
    return failures ? 1 : 0;
}
