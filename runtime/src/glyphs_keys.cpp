// Keyboard icons: the keycap archive with the player's own keys.
//
// tools/make_button_glyphs.py --styles keyboard writes, from the game's own ParkExt font, an archive with a keycap
// glyph for every label of every prompt (fonts/glyphs_keyboard/romfs/... is the fresh-binding fallback), plus:
//
//   fonts/glyphs_keyboard/base.sarc   the same archive, uncompressed, for patching in place
//   fonts/glyphs_keyboard/patch.txt   the cmap idDelta word of each prompt, and the block of label glyphs at it
//
// This points each prompt at the glyph of the key bound to its action (input.key.<action>, the settings screen's
// Controls page) and caches the result under fonts/glyphs_keyboard/cache/<hash>.sarc.zs, so a remap shows at the
// next start with nothing to regenerate. The game opens the archive once, while booting (glyphs.cpp).
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <map>
#include <mutex>
#include <string>

#include <nx/runtime.h>

#include "config.h"
#include "glyphs_patch.h"
#include "platform.h"

namespace fs = std::filesystem;

namespace nx {

namespace {

std::string read_all(const std::string& path) {
    FILE* f = plat::fopen_utf8(path, "rb");
    if (!f) return {};
    std::string s;
    char buf[65536];
    for (size_t n; (n = std::fread(buf, 1, sizeof buf, f)) > 0;) s.append(buf, n);
    std::fclose(f);
    return s;
}

bool write_all(const std::string& path, const std::string& data) {
    const std::string tmp = path + ".tmp";
    FILE* f = plat::fopen_utf8(tmp, "wb");
    if (!f) return false;
    bool ok = std::fwrite(data.data(), 1, data.size(), f) == data.size();
    ok = std::fclose(f) == 0 && ok;
    std::error_code ec;
    if (!ok) {
        fs::remove(fs::u8path(tmp), ec);
        return false;
    }
    fs::rename(fs::u8path(tmp), fs::u8path(path), ec);
    if (ec) {
        fs::remove(fs::u8path(path), ec);
        fs::rename(fs::u8path(tmp), fs::u8path(path), ec);
    }
    return !ec;
}

// The key bindings' defaults, as kKeyNames in platform_sdl.cpp (the only platform with a keyboard).
const char* default_key(const std::string& action) {
    if (action == "a") return "Space";
    if (action == "b") return "Left Shift";
    if (action == "x") return "X";
    if (action == "y") return "F";
    if (action == "l") return "Q";
    if (action == "r") return "E";
    if (action == "zl") return "Z";
    if (action == "zr") return "C";
    if (action == "plus") return "Return";
    if (action == "minus") return "Escape";
    return "";
}

// A binding (SDL's scancode name, as the settings screen stores it) as the label the keycap font draws. The names
// are the ones tools/make_button_glyphs.py has glyphs for (LABELS there); anything else shows "KEY".
std::string key_label(const std::string& binding) {
    std::string s = binding;
    if (s.rfind("Left ", 0) == 0) s = s.substr(5);         // "Left Shift", "Left Ctrl", "Left Alt"
    else if (s.rfind("Right ", 0) == 0) s = s.substr(6);
    if (s == "Space") return "SPC";
    if (s == "Shift") return "SHF";
    if (s == "Ctrl" || s == "Control") return "CTL";
    if (s == "Alt" || s == "AltGr") return "ALT";
    if (s == "Return" || s == "Enter" || s == "KP Enter") return "ENT";
    if (s == "Escape") return "ESC";
    if (s == "Tab") return "TAB";
    if (s == "Backspace") return "BSP";
    if (s == "Up") return "UP";
    if (s == "Down") return "DN";
    if (s == "Left") return "LT";
    if (s == "Right") return "RT";
    if (s == "PageUp") return "PGU";
    if (s == "PageDown") return "PGD";
    if (s == "Home") return "HOM";
    if (s == "End") return "END";
    if (s == "Insert") return "INS";
    if (s == "Delete") return "DEL";
    if (s.size() == 1 && std::isalnum(static_cast<unsigned char>(s[0])))
        return std::string(1, char(std::toupper(static_cast<unsigned char>(s[0]))));
    if (s.size() >= 2 && s.size() <= 3 && (s[0] == 'F' || s[0] == 'f') && std::isdigit(static_cast<unsigned char>(s[1])))
        return "F" + s.substr(1);  // F1..F12
    for (const char* prefix : {"KP ", "Keypad "}) {  // the keypad's digits
        size_t n = std::char_traits<char>::length(prefix);
        if (s.rfind(prefix, 0) == 0 && s.size() == n + 1 && std::isdigit(static_cast<unsigned char>(s[n])))
            return std::string("K") + s[n];
    }
    return "KEY";
}

constexpr const char* kActions[] = {"a", "b", "x", "y", "l", "r", "zl", "zr", "plus", "minus"};

}  // namespace

// The archive to serve in place of Font/ScalableFont.sarc.zs for the keyboard style: the cache for the current
// bindings, built now from the tool's base archive on first use; "" when the tool was never run for keyboard.
std::string keyboard_glyph_archive() {
    static std::mutex lock;
    static std::string served;
    static bool tried = false;
    std::lock_guard lk(lock);
    if (tried) return served;
    tried = true;

    const std::string root = plat::data_root() + "/fonts/glyphs_keyboard";
    glyphs::Manifest manifest;
    if (!manifest.load(read_all(root + "/patch.txt"))) {
        log("glyphs: keyboard icons need %s/patch.txt (run tools/make_button_glyphs.py --styles keyboard)",
            root.c_str());
        return served;
    }
    const std::string base = read_all(root + "/base.sarc");
    if (base.empty()) {
        log("glyphs: keyboard icons need %s/base.sarc (run tools/make_button_glyphs.py --styles keyboard)",
            root.c_str());
        return served;
    }

    std::map<std::string, std::string> bound;
    uint64_t hash = 1469598103934665603ull;  // FNV-1a over the labels: one cache file per set of keys
    for (const char* action : kActions) {
        std::string label = key_label(config_get(std::string("input.key.") + action, default_key(action)));
        bound[action] = label;
        for (unsigned char c : label) hash = (hash ^ c) * 1099511628211ull;
        hash = (hash ^ 0x20) * 1099511628211ull;
    }
    char name[32];
    std::snprintf(name, sizeof name, "%016llx", static_cast<unsigned long long>(hash));
    const std::string cache = root + "/cache/" + name + ".sarc.zs";

    std::error_code ec;
    if (fs::is_regular_file(fs::u8path(cache), ec)) {
        served = cache;
        log("glyphs: keyboard icons <- %s", cache.c_str());
        return served;
    }

    std::string archive = base;
    if (!glyphs::patch(archive, manifest, bound)) {
        log("glyphs: keyboard icons: %s/patch.txt does not match its archive; original icons are shown", root.c_str());
        return served;
    }
    fs::create_directories(fs::u8path(root + "/cache"), ec);
    if (!write_all(cache, glyphs::zstd_frame(archive))) {
        log("glyphs: keyboard icons: cannot write %s; original icons are shown", cache.c_str());
        return served;
    }
    served = cache;
    log("glyphs: keyboard icons <- %s (%zu prompts, keys %s %s %s %s %s %s %s %s %s %s)", cache.c_str(),
        manifest.entries.size(), bound["a"].c_str(), bound["b"].c_str(), bound["x"].c_str(), bound["y"].c_str(),
        bound["l"].c_str(), bound["r"].c_str(), bound["zl"].c_str(), bound["zr"].c_str(), bound["plus"].c_str(),
        bound["minus"].c_str());
    return served;
}

}  // namespace nx
