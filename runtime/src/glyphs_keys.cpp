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
//
// A folder ACNHRecomp-Importer prepared has no fonts/: the importer does not know this fork. When patch.txt is
// missing, the glyphs-gen beside the player (tools/make_button_glyphs.py, frozen by tools/package_glyphs_gen.sh)
// makes the archive from the folder's own RomFS on the first start, so nothing of the game ships.
#include <cctype>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <map>
#include <mutex>
#include <string>

#if defined(__linux__)
#include <fcntl.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>
#endif

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

#if defined(__linux__)
std::string utf8(const fs::path& p) {
    auto s = p.u8string();
    return std::string(s.begin(), s.end());
}
#endif

// Makes the keyboard archive from the game's own font, with the glyphs-gen the package carries beside the player:
// ACNHRecomp-Importer knows nothing of this fork's fonts/, so a folder it prepared has none. glyphs-gen is
// tools/make_button_glyphs.py frozen (tools/package_glyphs_gen.sh); it embeds no game data, it reads the font of
// the folder it is given. False when there is no glyphs-gen (a build from source) or no RomFS to read.
bool generate_keyboard_archive(const std::string& root) {
#if defined(__linux__)
    std::error_code ec;
    const fs::path self = fs::read_symlink("/proc/self/exe", ec);
    if (ec) return false;
    const fs::path tool = self.parent_path() / "glyphs-gen";
    if (!fs::is_regular_file(tool, ec)) return false;

    std::string romfs;  // an importer folder has romfs/; the setup's data folder has extract/romfs/
    for (const char* sub : {"romfs", "extract/romfs"}) {
        fs::path p = fs::u8path(plat::data_root()) / fs::u8path(sub);
        if (fs::is_regular_file(p / "Font" / "ScalableFont.sarc.zs", ec)) {
            romfs = utf8(p);
            break;
        }
    }
    if (romfs.empty()) return false;

    const std::string out = utf8(fs::u8path(root).parent_path());  // <data root>/fonts
    const std::string out_log = out + "/glyphs-gen.log";
    fs::create_directories(fs::u8path(out), ec);
    const std::string tool_s = utf8(tool);
    log("glyphs: keyboard icons are not generated yet: running glyphs-gen (first start, a few seconds)");

    // The generator runs in its own process, its output in fonts/glyphs-gen.log (there may be no terminal).
    posix_spawn_file_actions_t fa;
    posix_spawn_file_actions_init(&fa);
    posix_spawn_file_actions_addopen(&fa, 0, "/dev/null", O_RDONLY, 0);
    posix_spawn_file_actions_addopen(&fa, 1, out_log.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
    posix_spawn_file_actions_adddup2(&fa, 1, 2);
    char a_romfs[] = "--romfs", a_out[] = "--out", a_styles[] = "--styles", a_keyboard[] = "keyboard";
    char* argv[] = {const_cast<char*>(tool_s.c_str()), a_romfs, const_cast<char*>(romfs.c_str()), a_out,
                    const_cast<char*>(out.c_str()), a_styles, a_keyboard, nullptr};
    pid_t pid = -1;
    const int spawn = posix_spawn(&pid, tool_s.c_str(), &fa, nullptr, argv, environ);
    posix_spawn_file_actions_destroy(&fa);
    if (spawn != 0) {
        log("glyphs: cannot run %s (%s); the original icons are shown", tool_s.c_str(), std::strerror(spawn));
        return false;
    }
    int status = 0;
    pid_t waited;
    do {
        waited = waitpid(pid, &status, 0);
    } while (waited < 0 && errno == EINTR);
    if (waited < 0) {
        log("glyphs: glyphs-gen: waitpid: %s; the original icons are shown", std::strerror(errno));
        return false;
    }
    const int code = WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
    if (code != 0) {
        log("glyphs: glyphs-gen failed (%d): see %s; the original icons are shown", code, out_log.c_str());
        return false;
    }
    log("glyphs: keyboard icons generated in %s", out.c_str());
    return true;
#else
    (void)root;  // only the Linux package carries glyphs-gen
    return false;
#endif
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
    std::error_code ec;
    if (!fs::is_regular_file(fs::u8path(root + "/patch.txt"), ec))
        generate_keyboard_archive(root);  // a folder from ACNHRecomp-Importer has no fonts/
    glyphs::Manifest manifest;
    if (!manifest.load(read_all(root + "/patch.txt"))) {
        log("glyphs: keyboard icons need %s/patch.txt (glyphs-gen makes them; from source: "
            "tools/make_button_glyphs.py --styles keyboard)",
            root.c_str());
        return served;
    }
    const std::string base = read_all(root + "/base.sarc");
    if (base.empty()) {
        log("glyphs: keyboard icons need %s/base.sarc (glyphs-gen makes them; from source: "
            "tools/make_button_glyphs.py --styles keyboard)",
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
