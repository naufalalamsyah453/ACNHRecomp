// The keyboard icons' manifest, and the two byte-level steps the runtime does on it: point each prompt's cmap
// idDelta word at the glyph of the key actually bound, then pack the archive as a zstd frame of raw blocks (no
// libzstd: the game's decoder takes raw blocks, and the runtime only ever writes them).
//
// Kept clear of the runtime's headers so tests/glyph_patch_test.cpp links it alone (and CI runs that without any
// game data). tools/make_button_glyphs.py writes the manifest; see glyphs_keys.cpp for the paths and the flow.
#pragma once

#include <cstddef>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace nx::glyphs {

// One button prompt: the cmap idDelta word that says which keycap glyph it draws.
struct Entry {
    uint32_t cp = 0;        // the prompt's private-use code point (E0A0, E0E0, ...)
    std::string action;     // the input.key.<action> binding it follows ("a", "plus", ...)
    size_t offset = 0;      // the idDelta word, as an offset into the font's sfnt data
    uint32_t first_gid = 0; // glyph of labels[0] at this prompt; the label's glyph is first_gid + its index
};

struct Manifest {
    uint32_t key = 0;  // the BFTTF XOR key of the archive's font
    size_t base = 0;   // where that font starts in the archive (the BFTTF header is 8 bytes before it)
    std::vector<std::string> labels;
    std::vector<Entry> entries;
    bool load(const std::string& text);
};

// Writes (gid - cp) into every prompt's idDelta word, big endian, XORed back in with the BFTTF key (which runs over
// the 4-byte words from the font's start, so the word offset is what picks the key byte). False (and the archive
// may be half patched) when the manifest names a label the archive has no glyph for, or an offset past the end.
bool patch(std::string& archive, const Manifest& m, const std::map<std::string, std::string>& label_for_action);

// A zstd frame whose blocks are raw: magic, a single-segment header carrying the content size, then the bytes.
std::string zstd_frame(const std::string& data);

}  // namespace nx::glyphs
