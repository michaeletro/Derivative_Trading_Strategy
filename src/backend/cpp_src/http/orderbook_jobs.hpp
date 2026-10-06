#pragma once
// Linux/WSL: one fixed offline Python worker, never a shell or broker client.
#include "research_json.hpp"
#include "browser_auth.hpp"
#include <dts/replay.hpp>
#include <dts/time_series_store.hpp>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <filesystem>
#include <fcntl.h>
#include <fstream>
#include <mutex>
#include <set>
#include <spawn.h>
#include <string>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>

namespace dts::orderbook_jobs {
using Json = crow::json::wvalue;
using Read = crow::json::rvalue;
namespace fs = std::filesystem;
// Only integrity-checked worker output can use this response type. Preserve its
// exact JSON bytes: parsing/re-serializing doubles can corrupt tiny probabilities.
struct ArchivedResult { std::string bytes; };
inline bool job_id(const std::string& s) {
    return s.size() == 32 && s.find_first_not_of("0123456789abcdef") == std::string::npos;
}
inline std::int64_t now_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
}
inline std::string field(const Read& r, const char* key) {
    if (!r.has(key) || r[key].t() != crow::json::type::String) throw std::invalid_argument("Invalid research text field");
    return std::string(r[key].s());
}
inline void exact_fields(const Read& r, const std::set<std::string>& keys) {
    if (!r || r.t() != crow::json::type::Object || r.keys().size() != keys.size()) throw std::invalid_argument("Invalid research request fields");
    for (const auto& key : r.keys()) if (!keys.count(key)) throw std::invalid_argument("Unknown research field");
}
inline int bounded(const Read& r, const char* key, int lo, int hi) {
    if (!r.has(key) || r[key].t() != crow::json::type::Number) throw std::invalid_argument("Expected numeric research setting");
    const double n = r[key].d();
    if (!std::isfinite(n) || std::floor(n) != n || n < lo || n > hi) throw std::invalid_argument("Research integer outside bounds");
    return static_cast<int>(n);
}
inline void validate(const Read& r) {
    exact_fields(r, {"schema_version", "mode", "session_ids", "source", "configuration", "split"});
    bounded(r, "schema_version", 1, 1);
    const auto mode = field(r, "mode"), source = field(r, "source");
    if ((mode != "inspect" && mode != "compare" && mode != "describe" && mode != "flow" && mode != "dataset") || (source != "ibkr_tws" && source != "mock")) throw std::invalid_argument("Invalid research mode or source acknowledgement");
    const auto& ids = r["session_ids"];
    if (ids.t() != crow::json::type::List || ids.size() < 1 || ids.size() > 24) throw std::invalid_argument("Select 1..24 stopped sessions");
    std::set<std::string> seen;
    for (const auto& id : ids) {
        if (id.t() != crow::json::type::String) throw std::invalid_argument("Session IDs must be decimal strings");
        const std::string s = id.s();
        if (s.empty() || s.size() > 18 || s[0] == '0' || s.find_first_not_of("0123456789") != std::string::npos || !seen.insert(s).second) throw std::invalid_argument("Invalid or duplicate session ID");
    }
    const auto& c = r["configuration"];
    if (mode == "dataset") {
        exact_fields(c, {"levels", "return_seconds", "max_side_age_seconds"});
        bounded(c, "levels", 1, 5);
        const int horizon = bounded(c, "return_seconds", 60, 120);
        if (horizon != 60 && horizon != 120) throw std::invalid_argument("Dataset return interval must be 60 or 120 seconds");
        if (c["max_side_age_seconds"].t() != crow::json::type::Number || !std::isfinite(c["max_side_age_seconds"].d()) ||
            c["max_side_age_seconds"].d() <= 0 || c["max_side_age_seconds"].d() > 60)
            throw std::invalid_argument("Dataset side age must be positive and at most 60 seconds");
        if (r["split"].t() != crow::json::type::Null) throw std::invalid_argument("Dataset preparation does not accept model partitions");
        return;
    }
    if (mode == "flow") {
        exact_fields(c, {"bin_seconds", "start_seconds", "end_seconds", "clock_policy"});
        const auto number = [&](const char* key, double lo, double hi) {
            if (c[key].t() != crow::json::type::Number || !std::isfinite(c[key].d()) || c[key].d() < lo || c[key].d() > hi)
                throw std::invalid_argument("Invalid time-flow window or interval");
            return c[key].d();
        };
        number("bin_seconds", 0.1, 300.);
        const double start = number("start_seconds", 0., 86400.);
        if (start >= 86400.) throw std::invalid_argument("Time-flow start must be below 86400 seconds");
        if (c["end_seconds"].t() != crow::json::type::Null && number("end_seconds", 0., 86400.) <= start)
            throw std::invalid_argument("Time-flow end must follow start");
        const auto policy = field(c, "clock_policy");
        if (policy != "strict_receipt" && policy != "recorded_monotonic")
            throw std::invalid_argument("Choose an explicit time-flow clock policy");
        if (r["split"].t() != crow::json::type::Null)
            throw std::invalid_argument("Time-flow descriptions do not accept prediction partitions");
        return;
    }
    exact_fields(c, {"quantity", "levels", "step_seconds", "horizon_seconds", "lookback_seconds", "max_side_age_seconds", "target"});
    const int step = bounded(c, "step_seconds", 1, 60), horizon = bounded(c, "horizon_seconds", 1, 1800), lookback = bounded(c, "lookback_seconds", 2, 1800);
    bounded(c, "levels", 1, 10);
    if (horizon % step || lookback % step || 300 % step || lookback / step < 2) throw std::invalid_argument("Grid step must divide horizon, lookback and 300 seconds");
    for (const auto& pair : {std::make_pair("quantity", 1e9), std::make_pair("max_side_age_seconds", 60.)}) {
        if (c[pair.first].t() != crow::json::type::Number || !std::isfinite(c[pair.first].d()) || c[pair.first].d() <= 0 || c[pair.first].d() > pair.second) throw std::invalid_argument("Invalid quantity or stale-age setting");
    }
    const auto target = field(c, "target");
    if (target != "buy_cost_bps" && target != "sell_cost_bps" && target != "spread_bps") throw std::invalid_argument("Unsupported prediction target");
    if (mode == "inspect" || mode == "describe") {
        if (r["split"].t() != crow::json::type::Null) throw std::invalid_argument("Inspection and description do not fit a model or accept partitions");
    } else {
        const auto& split = r["split"]; exact_fields(split, {"train", "validation", "test"});
        std::string previous;
        for (const auto* part : {"train", "validation", "test"}) {
            const auto& days = split[part];
            if (days.t() != crow::json::type::List || days.size() < 1 || days.size() > 100) throw std::invalid_argument("Declare dates for train, validation and test");
            for (const auto& d : days) {
                if (d.t() != crow::json::type::String) throw std::invalid_argument("Date must be an ISO string");
                const std::string s = d.s();
                if (s.size() != 10 || s[4] != '-' || s[7] != '-' || s.find_first_not_of("0123456789-") != std::string::npos || (!previous.empty() && s <= previous)) throw std::invalid_argument("Dates must be unique and chronological across partitions");
                previous = s;
            }
        }
    }
}

inline void validate_variation(const Read& r) {
    exact_fields(r,{"schema_version","mode","input_kind","session_ids","snapshot_ids","source","configuration","split"});
    bounded(r,"schema_version",1,1);
    if(field(r,"mode")!="variation")throw std::invalid_argument("Invalid variation mode");
    const auto kind=field(r,"input_kind"),source=field(r,"source");
    if(kind!="bars"&&kind!="depth")throw std::invalid_argument("Select minute snapshots or stopped depth captures");
    for(const auto* key:{"session_ids","snapshot_ids"}) {
        const auto& ids=r[key];const auto cap=std::string(key)=="session_ids"?24:60;
        if(ids.t()!=crow::json::type::List||ids.size()>static_cast<std::size_t>(cap))throw std::invalid_argument("Too many variation inputs");
        std::set<std::string> seen;
        for(const auto& id:ids){
            if(id.t()!=crow::json::type::String)throw std::invalid_argument("Input IDs must be decimal strings");
            const std::string value=id.s();
            if(value.empty()||value.size()>18||value[0]=='0'||value.find_first_not_of("0123456789")!=std::string::npos||!seen.insert(value).second)
                throw std::invalid_argument("Invalid or repeated variation ID");
        }
    }
    if((kind=="bars"&&(r["snapshot_ids"].size()==0||r["session_ids"].size()!=0))||
       (kind=="depth"&&(r["session_ids"].size()==0||r["snapshot_ids"].size()!=0)))throw std::invalid_argument("Use one source family per study");
    if(kind=="bars"?(source!="ibkr_tws_historical"&&source!="synthetic_test"):(source!="ibkr_tws"&&source!="mock"))throw std::invalid_argument("Acknowledge the actual source");
    const auto& c=r["configuration"];exact_fields(c,{"step_seconds","horizon_minutes","max_side_age_seconds"});
    const int step=bounded(c,"step_seconds",5,300),minutes=bounded(c,"horizon_minutes",5,60);
    const std::set<int> steps=kind=="bars"?std::set<int>{60,300}:std::set<int>{5,10,15,30,60,300};
    if(!steps.count(step)||!std::set<int>{5,15,30,60}.count(minutes)||minutes*60/step<6)throw std::invalid_argument("Choose a supported grid with at least six returns per window");
    if(c["max_side_age_seconds"].t()!=crow::json::type::Number||!std::isfinite(c["max_side_age_seconds"].d())||c["max_side_age_seconds"].d()<=0||c["max_side_age_seconds"].d()>60)throw std::invalid_argument("Side age must be positive and at most 60 seconds");
    if(r["split"].t()!=crow::json::type::Null){
        exact_fields(r["split"],{"train","validation","test"});std::string previous;
        for(const auto* key:{"train","validation","test"}){
            const auto& dates=r["split"][key];if(dates.t()!=crow::json::type::List||dates.size()<1||dates.size()>60)throw std::invalid_argument("Supply explicit session dates for all partitions");
            for(const auto& d:dates){if(d.t()!=crow::json::type::String)throw std::invalid_argument("Expected ISO date");const std::string v=d.s();
                if(v.size()!=10||v[4]!='-'||v[7]!='-'||v.find_first_not_of("0123456789-")!=std::string::npos||v<=previous)throw std::invalid_argument("Use unique chronological ISO dates");
                previous=v;}
        }
    }
}

// Do not follow symlinks through a private report directory. The recorder already
// validated its parent directory; these checks independently protect job files.
inline void private_dir(const fs::path& p, bool create) {
    for (auto parent = p; !parent.empty(); parent = parent.parent_path()) {
        if (fs::is_symlink(fs::symlink_status(parent))) throw std::runtime_error("Research paths cannot use symlinks");
        if (parent == parent.parent_path()) break;
    }
    if (create && ::mkdir(p.c_str(), 0700) != 0 && errno != EEXIST) throw std::runtime_error("Cannot create private research directory");
    struct stat s{};
    if (::lstat(p.c_str(), &s) != 0 || !S_ISDIR(s.st_mode) || s.st_uid != ::geteuid() || (s.st_mode & 0077)) throw std::runtime_error("Research directory must be user-owned with mode 0700");
}
inline std::string read_file(const fs::path& p, std::size_t cap) {
    const int fd = ::open(p.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (fd < 0) throw std::out_of_range("Research file is unavailable");
    struct stat st{};
    if (::fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != ::geteuid() || (st.st_mode & 0077) || st.st_size < 0 || static_cast<std::size_t>(st.st_size) > cap) { ::close(fd); throw std::runtime_error("Invalid private research file or size"); }
    std::string out; char buffer[8192];
    for (;;) {
        const auto n = ::read(fd, buffer, sizeof(buffer));
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) { ::close(fd); throw std::runtime_error("Cannot read research result"); }
        if (!n) break;
        out.append(buffer, static_cast<std::size_t>(n));
        if (out.size() > cap) { ::close(fd); throw std::runtime_error("Research result exceeds size bound"); }
    }
    ::close(fd); return out;
}
inline void write_file(const fs::path& p, const std::string& text) {
    const int fd = ::open(p.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) throw std::runtime_error("Research file already exists or is unwritable");
    std::size_t offset = 0;
    while (offset < text.size()) {
        const auto n = ::write(fd, text.data() + offset, text.size() - offset);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { ::close(fd); throw std::runtime_error("Research write failed"); }
        offset += static_cast<std::size_t>(n);
    }
    const int done = ::fsync(fd); ::close(fd);
    if (done != 0) throw std::runtime_error("Research flush failed");
}
inline Read read_json(const fs::path& p, std::size_t cap) {
    const auto raw = read_file(p, cap); auto j = crow::json::load(raw);
    if (!j || j.t() != crow::json::type::Object) throw std::runtime_error("Invalid saved research JSON");
    return j;
}
inline void state_write(const fs::path& dir, const Json& value) {
    const auto tmp = dir / ("state-" + local_auth::random_secret().substr(0, 16) + ".tmp");
    write_file(tmp, value.dump()); fs::rename(tmp, dir / "state.json");
    const int fd = ::open(dir.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd >= 0) { const int status = ::fsync(fd); ::close(fd); if (status) throw std::runtime_error("Research directory flush failed"); }
}

// Stream a private export using short paginated reads through the recorder's
// EXISTING sole connection. Its exclusive SQLite lock is deliberately unchanged.
struct InputLimits {
    std::int64_t session_events;
    std::int64_t total_events;
    std::size_t file_bytes;
    std::size_t total_bytes;
};
inline InputLimits input_limits(const std::string& mode) {
    if (mode == "dataset") return InputLimits{10000000, 20000000, 4000000000ULL, 8000000000ULL};
    return (mode == "describe" || mode == "flow") ? InputLimits{500000, 500000, 200000000, 200000000}
                              : InputLimits{200000, 300000, 80000000, 120000000};
}
class SnapshotWriter {
    int fd_ = -1;
    std::size_t bytes_ = 0;
    const std::size_t max_bytes_;
    research::Sha256 hash_;
public:
    explicit SnapshotWriter(const fs::path& path, std::size_t max_bytes = 80000000) : max_bytes_(max_bytes) {
        fd_ = ::open(path.c_str(), O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
        if (fd_ < 0) throw std::runtime_error("Cannot create frozen capture");
    }
    ~SnapshotWriter() { if (fd_ >= 0) ::close(fd_); }
    void append(const std::string& text) {
        if (text.size() > max_bytes_ - bytes_) throw std::length_error("Capture exceeds the frozen-input size bound");
        bytes_ += text.size();
        hash_.update(text);
        std::size_t done = 0;
        while (done < text.size()) {
            const auto n = ::write(fd_, text.data() + done, text.size() - done);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) throw std::runtime_error("Frozen capture write failed");
            done += static_cast<std::size_t>(n);
        }
    }
    std::size_t finish() { if (::fsync(fd_)) throw std::runtime_error("Frozen capture flush failed"); ::close(fd_); fd_ = -1; return bytes_; }
    std::string sha256() const { return hash_.finish(); }
};
class DiskSpaceError : public std::length_error {
public:
    DiskSpaceError() : std::length_error("Insufficient free disk space for frozen inputs, dataset outputs and recorder reserve") {}
};
inline void require_space(const fs::path& dir, std::uint64_t required) {
    std::error_code error;
    const auto available = fs::space(dir, error).available;
    if (error || available < required) throw DiskSpaceError();
}
struct OwnedFile {
    int fd = -1;
    explicit OwnedFile(int value = -1) : fd(value) {}
    ~OwnedFile() { if (fd >= 0) ::close(fd); }
    OwnedFile(const OwnedFile&) = delete;
    OwnedFile& operator=(const OwnedFile&) = delete;
    OwnedFile(OwnedFile&& other) noexcept : fd(other.fd) { other.fd = -1; }
};
struct VerifiedArtifact {
    OwnedFile file;
    std::string filename, content_type, sha256;
    std::uint64_t bytes;
};
inline VerifiedArtifact verified_artifact(const fs::path& output, const Read& entry, const std::string& name) {
    // Only these worker-generated names are reachable; callers never supply a path.
    const std::map<std::string, std::pair<std::string, std::string>> allowed{
        {"features", {"features.csv.gz", "application/gzip"}}, {"minutes", {"minutes.csv.gz", "application/gzip"}},
        {"blocks", {"blocks.csv", "text/csv"}}, {"pairs", {"pairs.csv", "text/csv"}}};
    const auto found = allowed.find(name);
    if (found == allowed.end()) throw std::invalid_argument("Unknown dataset artifact");
    exact_fields(entry, {"name", "file", "sha256", "bytes", "rows", "content_type"});
    const auto& [filename, content_type] = found->second;
    if (field(entry, "name") != name || field(entry, "file") != filename || field(entry, "content_type") != content_type)
        throw std::runtime_error("Dataset artifact identity mismatch");
    const auto expected_hash = field(entry, "sha256");
    if (expected_hash.size() != 64 || expected_hash.find_first_not_of("0123456789abcdef") != std::string::npos)
        throw std::runtime_error("Dataset artifact hash is invalid");
    const auto length = static_cast<std::uint64_t>(bounded(entry, "bytes", 0, 256000000));
    private_dir(output, false);
    OwnedFile parent(::open(output.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
    if (parent.fd < 0) throw std::runtime_error("Dataset artifact directory is unavailable");
    OwnedFile source(::openat(parent.fd, filename.c_str(), O_RDONLY | O_NOFOLLOW | O_CLOEXEC));
    if (source.fd < 0) throw std::out_of_range("Dataset artifact unavailable");
    struct stat before{};
    if (::fstat(source.fd, &before) || !S_ISREG(before.st_mode) || before.st_uid != ::geteuid() ||
        (before.st_mode & 0077) || before.st_size < 0 || static_cast<std::uint64_t>(before.st_size) != length)
        throw std::runtime_error("Dataset artifact is not a private file of the expected size");
    require_space(output, length + 1000000000ULL);
    // Copy and hash once into an anonymous file. The exact accepted bytes are
    // subsequently served from this same owned inode, even if a local process
    // changes or replaces the saved output after verification. No large buffer.
    OwnedFile copy(::openat(parent.fd, ".", O_TMPFILE | O_RDWR | O_CLOEXEC, 0600));
    if (copy.fd < 0) throw std::runtime_error("Cannot create private verified download");
    research::Sha256 hash; std::array<char, 65536> buffer{}; std::uint64_t seen = 0;
    for (;;) {
        const auto n = ::read(source.fd, buffer.data(), buffer.size());
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) throw std::runtime_error("Cannot read dataset artifact");
        if (!n) break;
        seen += static_cast<std::uint64_t>(n);
        if (seen > length) throw std::runtime_error("Dataset artifact changed during verification");
        hash.update(buffer.data(), static_cast<std::size_t>(n));
        std::size_t written = 0;
        while (written < static_cast<std::size_t>(n)) {
            const auto amount = ::write(copy.fd, buffer.data() + written, static_cast<std::size_t>(n) - written);
            if (amount < 0 && errno == EINTR) continue;
            if (amount <= 0) throw std::runtime_error("Cannot prepare verified dataset download");
            written += static_cast<std::size_t>(amount);
        }
    }
    if (seen != length || hash.finish() != expected_hash) throw std::runtime_error("Dataset artifact integrity mismatch");
    if (::lseek(copy.fd, 0, SEEK_SET) < 0) throw std::runtime_error("Cannot rewind verified dataset download");
    return VerifiedArtifact{std::move(copy), filename, content_type, expected_hash, length};
}
inline Json row_json(const storage::Row& row) {
    Json out; for (const auto& [key, value] : row) std::visit([&](const auto& v) { out[key] = v; }, value); return out;
}

class Manager {
    storage::TimeSeriesStore& store_;
    fs::path root_, script_;
    std::string python_;
    std::mutex mutex_;
    std::condition_variable wake_;
    std::thread supervisor_;
    pid_t child_ = -1;
    std::string active_, pending_, requested_end_;
    std::chrono::steady_clock::time_point deadline_;
    std::atomic<bool> abort_preparation_{false};
    bool stopping_ = false;
    int timeout_seconds_;
    bool variation_;
    fs::path directory(const std::string& id) const {
        if (!job_id(id)) throw std::invalid_argument("Invalid research job ID");
        const auto p = root_ / id;
        if (!fs::exists(p)) throw std::out_of_range("Unknown research job");
        private_dir(p, false); return p;
    }
    void finish(const std::string& state, const std::string& error = "") {
        const auto dir = directory(active_); Json j(read_json(dir / "state.json", 16384));
        j["state"] = state; j["phase"] = "finished"; j["error"] = error; j["finished_ms"] = now_ms();
        if (state == "complete") j["result_sha256"] = research::sha256(read_file(dir / "result.json", 12000000));
        state_write(dir, j);
    }
    void freeze(const std::string& id, const std::string& request) {
        const auto r = crow::json::load(request); const auto dir = directory(id);
        const auto limits = input_limits(field(r, "mode"));
        const bool dataset = field(r, "mode") == "dataset";
        std::vector<Json> sources; std::size_t total_bytes = 0;
        std::int64_t total_events = 0;
        for (const auto& value : r["session_ids"]) {
            const std::string sid = value.s(); const auto numeric_id = std::stoll(sid);
            const auto session = store_.depth_session(numeric_id);
            if (std::get<std::string>(session.at("state")) == "recording") throw std::logic_error("Selected recording has not stopped");
            auto page = store_.depth_events(numeric_id, 0, 0, 1000); const auto through = page.through_id;
            Json outer; outer["schema_version"] = 1; outer["kind"] = "displayed_depth_export";
            outer["complete_exchange_book"] = false; outer["time_basis"] = "local_callback_receipt"; outer["exchange_timestamp"] = nullptr;
            if (dataset) outer["kind"] = "displayed_depth_stream";
            outer["session"] = row_json(session); outer["through_id"] = std::to_string(through);
            std::string prefix = outer.dump();
            if (dataset) prefix += '\n';
            else { prefix.pop_back(); prefix += ",\"events\":["; }
            const auto file = "capture-" + sid + (dataset ? ".jsonl" : ".json");
            SnapshotWriter out(dir / file, std::min(limits.file_bytes, limits.total_bytes - total_bytes)); out.append(prefix);
            std::int64_t last = 0, count = 0; bool first = true;
            for (;;) {
                if (abort_preparation_.load() || std::chrono::steady_clock::now() >= deadline_) throw std::runtime_error("Research preparation interrupted");
                if (dataset) require_space(dir, 2000000000ULL); // Output allowance plus recorder reserve.
                for (const auto& event : page.rows) {
                    const auto eid = std::stoll(std::get<std::string>(event.at("event_id")));
                    if (eid <= last || eid > through) throw std::runtime_error("Frozen event ordering failed");
                    if (++count > limits.session_events || ++total_events > limits.total_events) throw std::length_error("Frozen event limit exceeded");
                    if (!first && !dataset) out.append(",");
                    first = false; out.append(row_json(event).dump());
                    if (dataset) out.append("\n");
                    last = eid;
                }
                if (!page.has_more) break;
                if (page.rows.empty() || page.next_after_id != last) throw std::runtime_error("Frozen cursor did not advance");
                page = store_.depth_events(numeric_id, last, through, 1000);
                // Release the store mutex between pages so recording can progress.
                std::this_thread::yield();
            }
            if (count != std::stoll(std::get<std::string>(session.at("event_count"))) || store_.depth_session(numeric_id) != session) throw std::runtime_error("Frozen session metadata changed");
            if (!dataset) out.append("]}");
            const auto file_bytes = out.finish(); total_bytes += file_bytes;
            if (total_bytes > limits.total_bytes) throw std::length_error("Combined frozen inputs exceed the size bound");
            Json item; item["file"] = file; item["sha256"] = out.sha256();
            if (dataset) { item["kind"] = "depth_stream"; item["bytes"] = static_cast<std::uint64_t>(file_bytes);
                item["session_id"] = sid; item["event_count"] = std::to_string(count); item["through_id"] = std::to_string(through); }
            sources.push_back(std::move(item));
        }
        Json manifest; manifest["schema_version"] = 1; manifest["exports"] = std::move(sources);
        if(variation_){std::vector<Json> snapshots;
            for(const auto& value:r["snapshot_ids"]){
                if(abort_preparation_.load()||std::chrono::steady_clock::now()>=deadline_)throw std::runtime_error("Research preparation interrupted");
                const std::string sid=value.s();const auto file="snapshot-"+sid+".json";
                const auto raw=research_http::snapshot_json(store_.snapshot(std::stoll(sid)),true).dump();
                if(raw.size()>12000000)throw std::length_error("Snapshot exceeds input bound");
                total_bytes+=raw.size();if(total_bytes>120000000)throw std::length_error("Combined inputs exceed bound");
                write_file(dir/file,raw);Json item;item["file"]=file;item["sha256"]=research::sha256(raw);snapshots.push_back(std::move(item));
            }manifest["snapshots"]=std::move(snapshots);
        }
        write_file(dir / "inputs.json", manifest.dump());
    }
    bool spawn() {
        const auto dir = directory(active_);
        std::vector<std::string> args{python_, "-I", script_.string(), "--job-dir", dir.string(), "--parent-pid", std::to_string(::getpid())};
        std::vector<char*> argv; for (auto& arg : args) argv.push_back(arg.data()); argv.push_back(nullptr);
        std::vector<std::string> values{"PATH=/usr/bin:/bin", "LANG=C.UTF-8", "OPENBLAS_NUM_THREADS=1", "OMP_NUM_THREADS=1", "MKL_NUM_THREADS=1"};
        std::vector<char*> environment; for (auto& v : values) environment.push_back(v.data()); environment.push_back(nullptr);
        posix_spawn_file_actions_t actions; posix_spawnattr_t attributes;
        if (::posix_spawn_file_actions_init(&actions)) return false;
        if (::posix_spawnattr_init(&attributes)) { ::posix_spawn_file_actions_destroy(&actions); return false; }
        int error = 0;
        error |= ::posix_spawn_file_actions_addopen(&actions, 0, "/dev/null", O_RDONLY, 0);
        error |= ::posix_spawn_file_actions_addopen(&actions, 1, "/dev/null", O_WRONLY, 0);
        error |= ::posix_spawn_file_actions_addopen(&actions, 2, "/dev/null", O_WRONLY, 0);
        error |= ::posix_spawn_file_actions_addclosefrom_np(&actions, 3);
        error |= ::posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETPGROUP);
        error |= ::posix_spawnattr_setpgroup(&attributes, 0);
        pid_t pid = -1;
        if (!error) error = ::posix_spawn(&pid, python_.c_str(), &actions, &attributes, argv.data(), environment.data());
        ::posix_spawn_file_actions_destroy(&actions); ::posix_spawnattr_destroy(&attributes);
        if (error) return false;
        child_ = pid; return true;
    }
    void release() { child_ = -1; active_.clear(); pending_.clear(); requested_end_.clear(); }
    void observe() noexcept {
        std::unique_lock<std::mutex> lock(mutex_);
        while (!stopping_ || !active_.empty()) {
            if (!pending_.empty()) {
                const auto request = std::move(pending_), id = active_; pending_.clear();
                lock.unlock(); bool prepared = false;
                std::string preparation_error = "Could not freeze the selected completed captures within size/consistency bounds. The recorder archive was not changed.";
                try { freeze(id, request); prepared = true; }
                catch (const DiskSpaceError&) { preparation_error = "Insufficient free disk space for dataset preparation; recorder reserve and existing archives were preserved."; }
                catch (...) {}
                lock.lock();
                if (stopping_ && requested_end_.empty()) requested_end_ = "interrupted";
                if (std::chrono::steady_clock::now() >= deadline_ && requested_end_.empty()) requested_end_ = "timeout";
                try {
                    if (!requested_end_.empty()) { finish(requested_end_, "Research preparation stopped; the recorder archive was not changed."); release(); }
                    else if (!prepared) { finish("failed", preparation_error); release(); }
                    else if (!spawn()) { finish("failed", "Could not start the configured research interpreter."); release(); }
                    else { const auto dir = directory(active_); const auto saved = read_json(dir / "state.json", 16384); Json m(saved);
                        m["phase"] = field(saved, "mode") == "dataset" ? "building_dataset" : "modeling";
                        m["input_manifest_sha256"] = research::sha256(read_file(dir / "inputs.json", 16384)); state_write(dir, m); }
                } catch (...) { if (child_ > 0) { ::kill(-child_, SIGKILL); int c = 0; while (::waitpid(child_, &c, 0) < 0 && errno == EINTR) {} } release(); }
            }
            if (child_ > 0) {
                if (stopping_ && requested_end_.empty()) requested_end_ = "interrupted";
                if (std::chrono::steady_clock::now() >= deadline_ && requested_end_.empty()) requested_end_ = "timeout";
                if (!requested_end_.empty()) ::kill(-child_, SIGKILL);
                int code = 0; const auto ended = ::waitpid(child_, &code, WNOHANG);
                if (ended == child_ || (ended < 0 && errno != EINTR)) {
                    try {
                        if (!requested_end_.empty()) finish(requested_end_, "Research stopped; acquisition and stored market data were not changed.");
                        else if (ended == child_ && WIFEXITED(code) && WEXITSTATUS(code) == 0) { (void)read_json(directory(active_) / "result.json", 12000000); finish("complete"); }
                        else {
                            std::string error = "Research worker failed; check model dependencies, selected data and resource limits.";
                            try { error = field(read_json(directory(active_) / "error.json", 4096), "error").substr(0, 350); } catch (...) {}
                            finish("failed", error);
                        }
                    } catch (...) { try { finish("failed", "Research output did not validate; no result was accepted."); } catch (...) {} }
                    release();
                }
            }
            if (!stopping_ || !active_.empty()) wake_.wait_for(lock, std::chrono::milliseconds(100));
        }
    }
public:
    Manager(storage::TimeSeriesStore& store, std::string python, fs::path script, int timeout_seconds = 180, bool variation = false)
        : store_(store), root_(fs::path(store.status().database).parent_path() / (variation?"variation-research":"orderbook-research")), script_(std::move(script)),
          python_(std::move(python)), timeout_seconds_(timeout_seconds), variation_(variation) {
        if (timeout_seconds_ < 1 || timeout_seconds_ > 300) throw std::invalid_argument("Invalid research timeout");
        private_dir(root_, true);
        for (const auto& entry : fs::directory_iterator(root_)) if (job_id(entry.path().filename().string())) {
            try { private_dir(entry.path(), false); const auto r = read_json(entry.path() / "state.json", 16384);
                if (field(r, "state") == "running") { Json j(r); j["state"] = "interrupted"; j["phase"] = "finished"; j["finished_ms"] = now_ms(); j["error"] = "Server restarted; job was not resumed."; state_write(entry.path(), j); }
            } catch (...) {}
        }
        supervisor_ = std::thread([this] { observe(); });
    }
    ~Manager() { shutdown(); }
    void shutdown() noexcept {
        { std::lock_guard<std::mutex> lock(mutex_); stopping_ = true; abort_preparation_.store(true); }
        wake_.notify_all(); if (supervisor_.joinable()) supervisor_.join();
    }
    bool enabled() const { return !python_.empty() && fs::path(python_).is_absolute() && ::access(python_.c_str(), X_OK) == 0 && fs::is_regular_file(script_); }
    Json status() {
        std::lock_guard<std::mutex> lock(mutex_); Json j;
        j["schema_version"] = 1; j["enabled"] = enabled(); j["busy"] = !active_.empty();
        j["active_job_id"] = active_.empty() ? Json(nullptr) : Json(active_);
        j["timeout_seconds"] = timeout_seconds_; j["max_sessions"] = 24; j["max_events"] = 300000;
        if (!variation_) { j["describe_max_events"] = 500000; j["flow_max_events"] = 500000;
            j["dataset_max_session_events"] = 10000000; j["dataset_max_events"] = 20000000;
            j["dataset_max_file_bytes"] = 4000000000ULL; j["dataset_max_total_bytes"] = 8000000000ULL;
            j["dataset_timeout_seconds"] = 1800; j["dataset_artifact_max_bytes"] = 256000000;
            j["dataset_message"] = "Stopped captures are streamed in bounded pages; strict receipt-clock and usable-block checks remain required. Disk headroom is checked before preparation."; }
        if(variation_)j["max_snapshots"]=60;
        j["message"] = enabled() ? "Offline worker available; selected saved inputs are frozen before analysis. Dependencies and coverage are checked when a job runs." : "Launch with tools/start_dashboard.py from the model-enabled virtual environment to enable research jobs.";
        return j;
    }
    Json submit(const Read& request, storage::TimeSeriesStore& store) {
        if(variation_)validate_variation(request);else validate(request);
        std::lock_guard<std::mutex> lock(mutex_);
        if (!enabled() || stopping_) throw std::logic_error("Research worker unavailable; use the model-enabled launcher");
        if (!active_.empty()) throw std::length_error("A research job is already running; inspect it before submitting another");
        std::size_t jobs = 0; for (const auto& e : fs::directory_iterator(root_)) if (job_id(e.path().filename().string())) ++jobs;
        if (jobs >= 100) throw std::length_error("Research catalog reached 100 runs; archive old report directories explicitly before adding more");
        const auto limits = input_limits(field(request, "mode"));
        std::int64_t total = 0;
        for (const auto& id : request["session_ids"]) {
            const auto row = store.depth_session(std::stoll(std::string(id.s())));
            if (std::get<std::string>(row.at("state")) == "recording") throw std::logic_error("Stop all selected recordings before analysis");
            if (std::get<std::string>(row.at("source")) != field(request, "source")) throw std::invalid_argument("Selected source differs from the explicit source acknowledgement");
            const auto n = std::stoll(std::get<std::string>(row.at("event_count"))); total += n;
            if (n > limits.session_events || total > limits.total_events) throw std::length_error("Selected captures exceed the bounded research event limit");
        }
        if (field(request, "mode") == "dataset") {
            const auto estimate = std::min<std::uint64_t>(limits.total_bytes, static_cast<std::uint64_t>(total) * 1024 + request["session_ids"].size() * 4096);
            require_space(root_, estimate + 2000000000ULL);
        }
        if(variation_){std::size_t bars=0;for(const auto& value:request["snapshot_ids"]){
            const auto snap=store.snapshot(std::stoll(std::string(value.s())));
            if(snap.spec.bar_size!="1 min"||snap.spec.price_type!="MIDPOINT"||!snap.spec.use_rth||snap.source!=field(request,"source"))throw std::invalid_argument("Select same-source regular-hours minute MIDPOINT snapshots");
            bars+=snap.observations.size();if(bars>100000)throw std::length_error("More than 100000 bars selected");
        }}
        const auto id = local_auth::random_secret().substr(0, 32); const auto dir = root_ / id;
        if (::mkdir(dir.c_str(), 0700)) throw std::runtime_error("Cannot create research run");
        const auto raw = Json(request).dump(); write_file(dir / "request.json", raw);
        Json meta; meta["schema_version"] = 1; meta["job_id"] = id; meta["mode"] = field(request, "mode");
        meta["source"] = field(request, "source"); meta["session_ids"] = Json(request["session_ids"]); meta["request_sha256"] = research::sha256(raw);
        if(variation_){meta["snapshot_ids"]=Json(request["snapshot_ids"]);meta["input_kind"]=field(request,"input_kind");}
        meta["created_ms"] = now_ms(); meta["finished_ms"] = nullptr; meta["state"] = "running"; meta["phase"] = "snapshotting"; meta["error"] = "";
        state_write(dir, meta); active_ = id; pending_ = raw; abort_preparation_.store(false); requested_end_.clear();
        deadline_ = std::chrono::steady_clock::now() + std::chrono::seconds(field(request, "mode") == "dataset" ? 1800 : timeout_seconds_); wake_.notify_all(); return meta;
    }
    Json get(const std::string& id) { std::lock_guard<std::mutex> lock(mutex_); return Json(read_json(directory(id) / "state.json", 16384)); }
    Json list() {
        std::lock_guard<std::mutex> lock(mutex_); std::vector<std::pair<std::int64_t, std::string>> values; int invalid = 0;
        for (const auto& entry : fs::directory_iterator(root_)) if (job_id(entry.path().filename().string())) {
            try { private_dir(entry.path(), false); const auto s = read_json(entry.path() / "state.json", 16384); values.emplace_back(s["created_ms"].i(), entry.path().filename().string()); } catch (...) { ++invalid; }
        }
        std::sort(values.rbegin(), values.rend()); std::vector<Json> rows;
        for (std::size_t i = 0; i < std::min<std::size_t>(50, values.size()); ++i) rows.emplace_back(read_json(root_ / values[i].second / "state.json", 16384));
        Json j; j["schema_version"] = 1; j["jobs"] = std::move(rows); j["has_more"] = values.size() > 50; j["invalid_records"] = invalid; return j;
    }
    Json cancel(const std::string& id) {
        std::lock_guard<std::mutex> lock(mutex_); (void)directory(id); Json j; j["schema_version"] = 1; j["cancel_requested"] = active_ == id;
        if (active_ == id) { requested_end_ = "cancelled"; abort_preparation_.store(true); wake_.notify_all(); } return j;
    }
    ArchivedResult result(const std::string& id) {
        std::lock_guard<std::mutex> lock(mutex_); const auto dir = directory(id); const auto state = read_json(dir / "state.json", 16384);
        if (field(state, "state") != "complete") throw std::logic_error("Research job has no accepted completed result");
        const auto raw = read_file(dir / "result.json", 12000000);
        if (research::sha256(raw) != field(state, "result_sha256")) throw std::runtime_error("Research result integrity mismatch");
        const auto parsed = crow::json::load(raw);
        if (!parsed || parsed.t() != crow::json::type::Object) throw std::runtime_error("Invalid research result");
        return ArchivedResult{raw};
    }
    VerifiedArtifact artifact(const std::string& id, const std::string& name) {
        if (name != "features" && name != "minutes" && name != "blocks" && name != "pairs")
            throw std::invalid_argument("Unknown dataset artifact");
        const auto saved = result(id); // Validates completed state and exact saved JSON hash.
        const auto report = crow::json::load(saved.bytes);
        if (!report.has("request") || field(report["request"], "mode") != "dataset" ||
            !report.has("artifacts") || report["artifacts"].t() != crow::json::type::List || report["artifacts"].size() > 4)
            throw std::logic_error("This result has no dataset exports");
        const Read* selected = nullptr;
        for (const auto& item : report["artifacts"]) if (field(item, "name") == name) {
            if (selected) throw std::runtime_error("Repeated dataset artifact");
            selected = &item;
        }
        if (!selected) throw std::out_of_range("Dataset artifact unavailable");
        return verified_artifact(directory(id) / "output", *selected, name);
    }
};
} // namespace dts::orderbook_jobs
