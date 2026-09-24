#pragma once
// Local-only browser sessions. No credential is persisted here or in SQLite.
#include <array>
#include <cerrno>
#include <chrono>
#include <map>
#include <mutex>
#include <stdexcept>
#include <string>
#include <sys/random.h>

namespace dts::local_auth {
using Clock = std::chrono::steady_clock;
inline bool secret_shape(const std::string& value) {
    if (value.size() != 64) return false;
    for (const char c : value) if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    return true;
}
inline bool equal_secret(const std::string& a, const std::string& b) {
    if (a.size() != b.size()) return false;
    unsigned char difference = 0;
    for (std::size_t i = 0; i < a.size(); ++i)
        difference |= static_cast<unsigned char>(a[i]) ^ static_cast<unsigned char>(b[i]);
    return difference == 0;
}
inline std::string random_secret() {
    std::array<unsigned char, 32> bytes{};
    std::size_t filled = 0;
    while (filled != bytes.size()) {
        const auto n = ::getrandom(bytes.data() + filled, bytes.size() - filled, 0);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) throw std::runtime_error("Local session entropy unavailable");
        filled += static_cast<std::size_t>(n);
    }
    const char* hex = "0123456789abcdef";
    std::string result; result.reserve(64);
    for (auto b : bytes) { result += hex[b >> 4]; result += hex[b & 15]; }
    return result;
}

class Sessions {
    struct Entry { std::string host; Clock::time_point expires; };
    std::mutex mutex_;
    std::map<std::string, Entry> tickets_, sessions_;
    static void prune(std::map<std::string, Entry>& entries, Clock::time_point now) {
        for (auto it = entries.begin(); it != entries.end();) {
            if (it->second.expires <= now) it = entries.erase(it); else ++it;
        }
    }
public:
    static constexpr auto ticket_lifetime = std::chrono::seconds(60);
    static constexpr auto session_lifetime = std::chrono::hours(12);
    // The launcher supplies a one-use code, not the profile credential, to its
    // browser helper. Its expiry begins only after application initialization.
    void seed(const std::string& code, const std::string& host, Clock::time_point now = Clock::now()) {
        if (!secret_shape(code)) throw std::invalid_argument("Invalid local launch code");
        std::lock_guard<std::mutex> lock(mutex_); prune(tickets_, now);
        if (tickets_.size() >= 8) throw std::length_error("Too many pending local sign-ins");
        if (!tickets_.emplace(code, Entry{host, now + ticket_lifetime}).second)
            throw std::invalid_argument("Duplicate local launch code");
    }
    std::string issue(const std::string& host, Clock::time_point now = Clock::now()) {
        const auto code = random_secret(); seed(code, host, now); return code;
    }
    // A ticket is host-bound and consumed atomically, once. It cannot be used
    // as an API bearer credential. Existing browser sessions are not evicted.
    std::string exchange(const std::string& code, const std::string& host,
                         const std::string& previous = "", Clock::time_point now = Clock::now()) {
        std::lock_guard<std::mutex> lock(mutex_); prune(tickets_, now); prune(sessions_, now);
        const auto it = tickets_.find(code);
        if (!secret_shape(code) || it == tickets_.end() || it->second.host != host) return {};
        const auto old = sessions_.find(previous);
        const bool replace = old != sessions_.end() && old->second.host == host;
        if (sessions_.size() >= 32 && !replace) throw std::length_error("Too many local browser sessions");
        const auto session = random_secret();
        sessions_.emplace(session, Entry{host, now + session_lifetime});
        if (replace) sessions_.erase(old);
        tickets_.erase(it); return session;
    }
    bool valid(const std::string& session, const std::string& host, Clock::time_point now = Clock::now()) {
        std::lock_guard<std::mutex> lock(mutex_); prune(sessions_, now);
        const auto it = sessions_.find(session);
        return secret_shape(session) && it != sessions_.end() && it->second.host == host;
    }
    void revoke(const std::string& session, const std::string& host) {
        std::lock_guard<std::mutex> lock(mutex_);
        const auto it = sessions_.find(session);
        if (it != sessions_.end() && it->second.host == host) sessions_.erase(it);
    }
};

inline std::string cookie_name(int port) { return "dts_local_session_" + std::to_string(port); }
inline std::string cookie_value(const std::string& header, int port) {
    if (header.size() > 8192) return {};
    const auto name = cookie_name(port) + "=";
    std::string result; bool found = false;
    for (std::size_t start = 0; start < header.size();) {
        const auto end = header.find(';', start);
        const auto stop = end == std::string::npos ? header.size() : end;
        while (start < stop && (header[start] == ' ' || header[start] == '\t')) ++start;
        auto item = header.substr(start, stop - start);
        if (item.compare(0, name.size(), name) == 0) {
            if (found) return {}; // Reject duplicate/ambiguous session cookies.
            result = item.substr(name.size()); found = true;
        }
        start = stop + 1;
    }
    return secret_shape(result) ? result : std::string();
}
inline std::string set_cookie(const std::string& value, int port) {
    if (!secret_shape(value)) throw std::invalid_argument("Invalid local session");
    // HTTP loopback only. No Secure claim or __Host- prefix without HTTPS.
    // Cookies are NOT port-isolated; all local services must be trusted.
    return cookie_name(port) + "=" + value + "; Path=/; HttpOnly; SameSite=Strict";
}
inline std::string clear_cookie(int port) {
    return cookie_name(port) + "=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0";
}
} // namespace dts::local_auth
