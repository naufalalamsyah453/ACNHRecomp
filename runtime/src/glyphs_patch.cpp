// See glyphs_patch.h: the manifest parser, the cmap idDelta patch and the raw-block zstd frame writer.
#include "glyphs_patch.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace nx::glyphs {
namespace {

std::vector<std::string> split(const std::string& s, char sep) {
    std::vector<std::string> out;
    size_t pos = 0;
    while (true) {
        size_t at = s.find(sep, pos);
        out.push_back(s.substr(pos, at == std::string::npos ? std::string::npos : at - pos));
        if (at == std::string::npos) break;
        pos = at + 1;
    }
    return out;
}

bool starts_with(const std::string& s, const char* with) { return s.compare(0, std::strlen(with), with) == 0; }

}  // namespace

bool Manifest::load(const std::string& text) {
    key = 0;
    base = 0;
    labels.clear();
    entries.clear();
    size_t pos = 0;
    while (pos <= text.size()) {
        size_t nl = text.find('\n', pos);
        std::string line = text.substr(pos, nl == std::string::npos ? std::string::npos : nl - pos);
        pos = nl == std::string::npos ? text.size() + 1 : nl + 1;
        while (!line.empty() && (line.back() == '\r' || line.back() == ' ' || line.back() == '\t')) line.pop_back();
        if (line.empty() || line[0] == '#') continue;
        if (starts_with(line, "key ")) {
            unsigned v = 0;
            if (std::sscanf(line.c_str() + 4, "%x", &v) != 1) return false;
            key = v;
        } else if (starts_with(line, "base ")) {
            long long v = 0;
            if (std::sscanf(line.c_str() + 5, "%lld", &v) != 1 || v < 0) return false;
            base = size_t(v);
        } else if (starts_with(line, "labels ")) {
            labels = split(line.substr(7), ',');
            if (labels.empty()) return false;
        } else if (starts_with(line, "cp ")) {
            unsigned cp = 0, gid = 0;
            char action[32] = {};
            long long offset = 0;
            if (std::sscanf(line.c_str() + 3, "%x %31s %lld %u", &cp, action, &offset, &gid) != 4) return false;
            if (offset < 0) return false;
            entries.push_back(Entry{cp, action, size_t(offset), gid});
        } else {
            return false;  // a manifest from a newer tool: do not guess
        }
    }
    return !labels.empty() && !entries.empty();
}

bool patch(std::string& archive, const Manifest& m, const std::map<std::string, std::string>& label_for_action) {
    if (m.key == 0 || m.labels.empty()) return false;
    const uint8_t k[4] = {uint8_t(m.key), uint8_t(m.key >> 8), uint8_t(m.key >> 16), uint8_t(m.key >> 24)};
    for (const Entry& e : m.entries) {
        auto it = label_for_action.find(e.action);
        if (it == label_for_action.end()) return false;
        auto at = std::find(m.labels.begin(), m.labels.end(), it->second);
        if (at == m.labels.end()) return false;  // the key has no keycap in this archive
        size_t at_byte = m.base + e.offset;
        if (at_byte < m.base || at_byte + 2 > archive.size()) return false;
        size_t index = size_t(at - m.labels.begin());
        uint16_t delta = uint16_t((e.first_gid + uint32_t(index) - e.cp) & 0xFFFF);
        // The archive is the BFTTF-encoded font: every 4-byte word is XORed with the key, from the font's start.
        archive[at_byte] = char(uint8_t(delta >> 8) ^ k[e.offset % 4]);
        archive[at_byte + 1] = char(uint8_t(delta & 0xFF) ^ k[(e.offset + 1) % 4]);
    }
    return true;
}

std::string zstd_frame(const std::string& data) {
    constexpr size_t kBlock = 128 * 1024;  // raw blocks: the format's limit
    std::string out;
    out.reserve(data.size() + data.size() / kBlock * 4 + 16);
    // Magic 0xFD2FB528, little endian.
    out.push_back(char(0x28));
    out.push_back(char(0xB5));
    out.push_back(char(0x2F));
    out.push_back(char(0xFD));
    // Frame header: single segment (bit 5), Frame_Content_Size with 1, 2 or 4 bytes (bits 7-6), no checksum, no
    // dictionary. With a single segment the window size is the content size, so every raw block fits.
    const size_t n = data.size();
    if (n < 256) {
        out.push_back(char(0x20));
        out.push_back(char(uint8_t(n)));
    } else if (n < 65792) {
        uint32_t v = uint32_t(n) - 256;
        out.push_back(char(0x60));
        out.push_back(char(uint8_t(v)));
        out.push_back(char(uint8_t(v >> 8)));
    } else {
        uint32_t v = uint32_t(n);
        out.push_back(char(0xA0));
        for (int i = 0; i < 4; i++) out.push_back(char(uint8_t(v >> (8 * i))));
    }
    size_t pos = 0;
    do {
        size_t take = std::min(kBlock, n - pos);
        uint32_t header = (uint32_t(take) << 3) | (pos + take >= n ? 1u : 0u);  // block type 0: raw
        out.push_back(char(uint8_t(header)));
        out.push_back(char(uint8_t(header >> 8)));
        out.push_back(char(uint8_t(header >> 16)));
        out.append(data, pos, take);
        pos += take;
    } while (pos < n);
    return out;
}

}  // namespace nx::glyphs
