#pragma once
#include "orderbook_jobs.hpp"
#include <dts/read_only_service.hpp>
#include <functional>

namespace dts::collection_http {
using orderbook_jobs::Json;
using orderbook_jobs::Read;
namespace fs = std::filesystem;

// All calls share the application's broker mutex. This owns only displayed-depth
// subscriptions; it never connects, reconnects, resumes or submits an order.
class Manager {
public:
    using ClockSource = std::function<Clock::time_point()>;
    using WallClockSource = std::function<std::int64_t()>;
private:
    struct Segment {
        int index = 0;
        std::string state = "starting", reason, session_id, archive_state;
        RequestId request = 0;
        std::int64_t started_ms = 0, ended_ms = 0;
        double elapsed = 0;
        bool finalized = false, gap = false;
        std::string event_count = "0", sequence = "0", raw_sequence;
        std::uint64_t updates = 0;
    };
    struct Plan {
        std::string id, state = "starting", reason, source, symbol, currency, venue, recorder_run_id;
        std::string schedule_id, session_date;
        std::int64_t collection_open_ms = 0, collection_close_ms = 0;
        ContractId contract_id = 0;
        int rows = 0, seconds = 0, count = 0;
        std::int64_t created_ms = 0, updated_ms = 0, finished_ms = 0;
        std::vector<Segment> segments;
        std::vector<std::string> warnings;
        std::string quality;
        std::optional<double> update_age;
        bool checkpoint_metrics_incomplete = false;
    };
    storage::TimeSeriesStore& store_;
    ClockSource clock_;
    WallClockSource wall_;
    fs::path root_, history_;
    std::map<std::string, Plan> plans_;
    std::string active_, current_;
    std::size_t journal_count_ = 0, invalid_ = 0;
    std::uint64_t generation_ = 0;
    Clock::time_point segment_started_{}, next_health_{}, last_tick_{};
    Clock::time_point collection_close_deadline_{};
    std::string expected_account_;
    static constexpr std::size_t meta_cap = 131072, plan_cap = 100;

    static bool live(const std::string& state) {
        return state == "starting" || state == "recording" || state == "stopping";
    }
    static bool paper_account(const std::string& value) {
        return value.size() >= 3 && value.size() <= 32 && value.compare(0, 2, "DU") == 0 &&
            value.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") == std::string::npos;
    }
    static bool iso_date(const std::string& value) {
        if (value.size() != 10) return false;
        for (std::size_t i = 0; i < value.size(); ++i)
            if ((i == 4 || i == 7) ? value[i] != '-' : (value[i] < '0' || value[i] > '9')) return false;
        const int year = std::stoi(value.substr(0, 4)), month = std::stoi(value.substr(5, 2)), day = std::stoi(value.substr(8, 2));
        const int days[] = {0,31,28,31,30,31,30,31,31,30,31,30,31};
        const bool leap = year % 400 == 0 || (year % 4 == 0 && year % 100 != 0);
        return year >= 1970 && year <= 2100 && month >= 1 && month <= 12 && day >= 1 && day <= days[month] + (month == 2 && leap ? 1 : 0);
    }
    static bool account_matches(const ReadOnlyService& service, const std::string& account) {
        const auto& accounts = service.trading_accounts();
        return paper_account(account) && accounts.status == SnapshotStatus::Complete &&
            accounts.accounts.size() == 1 && accounts.accounts.front() == account;
    }
    bool close_due(const Plan& p, Clock::time_point now) const {
        return !p.schedule_id.empty() && (wall_() >= p.collection_close_ms || now >= collection_close_deadline_);
    }
    static std::string code(const std::string& value, const char* fallback) {
        if (value.empty() || value.size() > 80 || value.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789_") != std::string::npos)
            return fallback;
        return value;
    }
    static std::string text(const storage::Row& row, const char* key) {
        const auto& value = row.at(key);
        if (const auto* p = std::get_if<std::string>(&value)) return *p;
        if (const auto* p = std::get_if<std::int64_t>(&value)) return std::to_string(*p);
        throw std::runtime_error("Missing collection archive evidence");
    }
    static bool decimal(const std::string& value, bool zero = true) {
        return !value.empty() && value.size() <= 19 && value.find_first_not_of("0123456789") == std::string::npos &&
            (zero || value != "0");
    }
    static double number(const Read& r, const char* key, double maximum = 172800.) {
        if (!r.has(key) || r[key].t() != crow::json::type::Number || !std::isfinite(r[key].d()) || r[key].d() < 0 || r[key].d() > maximum)
            throw std::runtime_error("Invalid saved collection number");
        return r[key].d();
    }
    static bool boolean(const Read& r, const char* key) {
        if (!r.has(key) || (r[key].t() != crow::json::type::True && r[key].t() != crow::json::type::False))
            throw std::invalid_argument("Expected a collection boolean flag");
        return r[key].b();
    }
    static std::string nullable_text(const Read& r, const char* key) {
        return r[key].t() == crow::json::type::Null ? "" : orderbook_jobs::field(r, key);
    }
    static std::int64_t timestamp(const Read& r, const char* key, bool nullable = false) {
        if (nullable && r[key].t() == crow::json::type::Null) return 0;
        if (r[key].t() != crow::json::type::Number || r[key].d() < 1 || r[key].d() > 253402300799999. || std::floor(r[key].d()) != r[key].d())
            throw std::runtime_error("Invalid saved collection timestamp");
        return r[key].i();
    }
    fs::path directory(const std::string& id, bool archived = false) const {
        if (!orderbook_jobs::job_id(id)) throw std::invalid_argument("Invalid collection ID");
        const auto dir = (archived ? history_ : root_) / id;
        orderbook_jobs::private_dir(dir, false);
        return dir;
    }
    Json segment_json(const Segment& s) const {
        Json j;
        j["index"] = s.index; j["state"] = s.state; j["reason"] = s.reason;
        j["request_id"] = s.request ? Json(std::to_string(s.request)) : Json(nullptr);
        j["session_id"] = s.session_id.empty() ? Json(nullptr) : Json(s.session_id);
        j["started_ms"] = s.started_ms; j["ended_ms"] = s.ended_ms ? Json(s.ended_ms) : Json(nullptr);
        j["elapsed_seconds"] = s.elapsed; j["finalized"] = s.finalized; j["gap"] = s.gap;
        j["archive_state"] = s.archive_state.empty() ? Json(nullptr) : Json(s.archive_state);
        j["committed_event_count"] = s.event_count; j["committed_sequence"] = s.sequence;
        j["raw_committed_sequence"] = s.raw_sequence.empty() ? Json(nullptr) : Json(s.raw_sequence);
        j["updates_observed"] = std::to_string(s.updates); j["delivery_observed"] = s.updates > 0;
        return j;
    }
    Json plan_json(const Plan& p) const {
        Json j;
        j["collection_id"] = p.id; j["state"] = p.state; j["reason"] = p.reason; j["source"] = p.source;
        j["schedule_id"] = p.schedule_id.empty() ? Json(nullptr) : Json(p.schedule_id);
        j["session_date"] = p.session_date.empty() ? Json(nullptr) : Json(p.session_date);
        j["collection_open_ms"] = p.collection_open_ms ? Json(p.collection_open_ms) : Json(nullptr);
        j["collection_close_ms"] = p.collection_close_ms ? Json(p.collection_close_ms) : Json(nullptr);
        j["contract_id"] = std::to_string(p.contract_id); j["symbol"] = p.symbol; j["currency"] = p.currency;
        j["venue"] = p.venue; j["rows"] = p.rows; j["segment_seconds"] = p.seconds;
        j["segments_requested"] = p.count;
        j["segments_completed"] = static_cast<int>(std::count_if(p.segments.begin(), p.segments.end(), [](const auto& s) { return s.finalized; }));
        j["created_ms"] = p.created_ms; j["updated_ms"] = p.updated_ms;
        j["finished_ms"] = p.finished_ms ? Json(p.finished_ms) : Json(nullptr);
        j["paper_read_only_confirmed"] = true; j["requested_seconds"] = p.seconds * p.count;
        j["checkpoint_metrics_incomplete"] = p.checkpoint_metrics_incomplete;
        double elapsed = 0; for (const auto& s : p.segments) elapsed += s.elapsed;
        j["elapsed_seconds"] = elapsed; j["remaining_seconds"] = std::max(0., static_cast<double>(p.seconds * p.count) - elapsed);
        j["current_segment"] = live(p.state) && !p.segments.empty() ? Json(p.segments.back().index) : Json(nullptr);
        std::vector<Json> rows; for (const auto& s : p.segments) rows.push_back(segment_json(s));
        j["segments"] = std::move(rows); j["warnings"] = p.warnings;
        j["book_quality"] = p.quality.empty() ? Json(nullptr) : Json(p.quality);
        j["last_update_age_seconds"] = p.update_age ? Json(*p.update_age) : Json(nullptr);
        return j;
    }
    void persist(Plan& p) {
        p.updated_ms = orderbook_jobs::now_ms();
        auto j = plan_json(p); j["schema_version"] = 1;
        j["recorder_run_id"] = p.recorder_run_id;
        orderbook_jobs::state_write(directory(p.id), j);
    }
    Plan load(const std::string& id, bool archived = false) const {
        const auto r = orderbook_jobs::read_json(directory(id, archived) / "state.json", meta_cap);
        if (orderbook_jobs::bounded(r, "schema_version", 1, 1) != 1 || orderbook_jobs::field(r, "collection_id") != id)
            throw std::runtime_error("Invalid saved collection identity");
        Plan p; p.id = id; p.state = orderbook_jobs::field(r, "state"); p.reason = orderbook_jobs::field(r, "reason");
        if (!live(p.state) && p.state != "completed" && p.state != "stopped" && p.state != "interrupted") throw std::runtime_error("Invalid saved collection state");
        if ((!p.reason.empty() && code(p.reason, "") != p.reason) || !boolean(r, "paper_read_only_confirmed")) throw std::runtime_error("Invalid saved collection assertion");
        p.source = orderbook_jobs::field(r, "source");
        if (p.source != "ibkr_tws" && p.source != "mock") throw std::runtime_error("Invalid saved collection source");
        p.recorder_run_id = orderbook_jobs::field(r, "recorder_run_id");
        if (!decimal(p.recorder_run_id, false)) throw std::runtime_error("Invalid saved recorder identity");
        const auto cid = orderbook_jobs::field(r, "contract_id");
        if (!decimal(cid, false)) throw std::runtime_error("Invalid saved collection contract");
        p.contract_id = std::stoll(cid);
        p.symbol = orderbook_jobs::field(r, "symbol"); p.currency = orderbook_jobs::field(r, "currency"); p.venue = orderbook_jobs::field(r, "venue");
        if (p.contract_id <= 0 || p.symbol.empty() || p.symbol.size() > 32 || p.currency != "USD" || (p.venue != "BATS" && p.venue != "IEX")) throw std::runtime_error("Invalid saved collection instrument");
        p.rows = orderbook_jobs::bounded(r, "rows", 1, 50); p.seconds = orderbook_jobs::bounded(r, "segment_seconds", 60, 3600);
        if (r.has("schedule_id") && r["schedule_id"].t() != crow::json::type::Null) {
            p.schedule_id = orderbook_jobs::field(r, "schedule_id"); p.session_date = orderbook_jobs::field(r, "session_date");
            p.collection_open_ms = timestamp(r, "collection_open_ms"); p.collection_close_ms = timestamp(r, "collection_close_ms");
            if (!orderbook_jobs::job_id(p.schedule_id) || !iso_date(p.session_date) || p.source != "ibkr_tws" ||
                p.contract_id != 756733 || p.symbol != "SPY" || p.venue != "BATS" || p.rows != 50 || p.seconds != 900 ||
                p.collection_close_ms <= p.collection_open_ms || p.collection_close_ms - p.collection_open_ms > 27000000)
                throw std::runtime_error("Invalid saved scheduled collection");
        }
        p.count = orderbook_jobs::bounded(r, "segments_requested", 1, p.schedule_id.empty() ? 16 : 30);
        if (p.count * p.seconds > (p.schedule_id.empty() ? 14400 : 27000)) throw std::runtime_error("Invalid saved collection duration");
        p.created_ms = timestamp(r, "created_ms"); p.updated_ms = timestamp(r, "updated_ms"); p.finished_ms = timestamp(r, "finished_ms", true);
        p.checkpoint_metrics_incomplete = r.has("checkpoint_metrics_incomplete") ? boolean(r, "checkpoint_metrics_incomplete") : false;
        if (r["segments"].t() != crow::json::type::List || r["segments"].size() > static_cast<std::size_t>(p.count)) throw std::runtime_error("Invalid saved collection segments");
        for (const auto& row : r["segments"]) {
            Segment s; s.index = orderbook_jobs::bounded(row, "index", 1, p.schedule_id.empty() ? 16 : 30);
            if (s.index != static_cast<int>(p.segments.size()) + 1) throw std::runtime_error("Invalid saved segment ordering");
            s.state = orderbook_jobs::field(row, "state"); s.reason = orderbook_jobs::field(row, "reason");
            if (!live(s.state) && s.state != "stopped" && s.state != "interrupted") throw std::runtime_error("Invalid saved segment state");
            const auto request = nullable_text(row, "request_id"); s.session_id = nullable_text(row, "session_id");
            if ((!request.empty() && !decimal(request, false)) || (!s.session_id.empty() && !decimal(s.session_id, false))) throw std::runtime_error("Invalid saved segment identity");
            if (!request.empty()) s.request = std::stoull(request);
            s.started_ms = timestamp(row, "started_ms"); s.ended_ms = timestamp(row, "ended_ms", true);
            s.elapsed = number(row, "elapsed_seconds"); s.finalized = boolean(row, "finalized"); s.gap = boolean(row, "gap");
            s.archive_state = nullable_text(row, "archive_state"); s.event_count = orderbook_jobs::field(row, "committed_event_count");
            s.sequence = orderbook_jobs::field(row, "committed_sequence"); s.raw_sequence = nullable_text(row, "raw_committed_sequence");
            const auto updates = orderbook_jobs::field(row, "updates_observed");
            for (const auto& value : {s.event_count, s.sequence, updates}) if (!decimal(value)) throw std::runtime_error("Invalid saved segment count");
            if (!s.raw_sequence.empty() && !decimal(s.raw_sequence)) throw std::runtime_error("Invalid saved raw watermark");
            s.updates = std::stoull(updates);
            if (s.finalized && (s.state != "stopped" || s.archive_state != "stop" || s.gap || !s.ended_ms || s.session_id.empty() || s.sequence != s.raw_sequence || s.sequence == "0")) throw std::runtime_error("Invalid saved stop evidence");
            p.segments.push_back(std::move(s));
        }
        return p;
    }
    void elapsed(Plan& p, Clock::time_point now) {
        if (!p.segments.empty() && live(p.segments.back().state))
            p.segments.back().elapsed = std::max(p.segments.back().elapsed, std::max(0., std::chrono::duration<double>(now - segment_started_).count()));
    }
    void evidence(Plan& p, Segment& s, bool archived = false) {
        if (!s.request) return;
        if (archived && s.session_id.empty()) throw std::runtime_error("Missing saved collection session identity");
        const auto row = archived ? store_.depth_session(std::stoll(s.session_id)) : store_.depth_recording(p.source, s.request);
        if (row.empty() || text(row, "run_id") != p.recorder_run_id || text(row, "native_id") != std::to_string(s.request) || text(row, "contract_id") != std::to_string(p.contract_id) || text(row, "venue") != p.venue || text(row, "requested_rows") != std::to_string(p.rows) || text(row, "source") != p.source)
            throw std::runtime_error("Collection archive identity mismatch");
        s.session_id = text(row, "session_id"); s.archive_state = text(row, "state");
        s.event_count = text(row, "event_count"); s.sequence = text(row, "last_sequence");
        const auto& ended = row.at("ended_ms");
        s.ended_ms = std::holds_alternative<std::int64_t>(ended) ? std::get<std::int64_t>(ended) : 0;
        const auto raw = store_.depth_raw_metadata(std::stoll(s.session_id));
        s.raw_sequence = std::get<std::int64_t>(raw.at("available")) ? text(raw, "committed_through_sequence") : "";
    }
    void finish(Plan& p, const std::string& state, const std::string& reason) {
        p.state = state; p.reason = reason; p.finished_ms = orderbook_jobs::now_ms();
        persist(p); active_.clear(); expected_account_.clear();
    }
    void make_room() {
        if (journal_count_ < plan_cap) return;
        auto candidate = plans_.end();
        for (auto it = plans_.begin(); it != plans_.end(); ++it) {
            if (live(it->second.state) || it->first == active_ || it->first == current_) continue;
            if (candidate == plans_.end() || std::tie(it->second.created_ms, it->second.id) <
                std::tie(candidate->second.created_ms, candidate->second.id)) candidate = it;
        }
        if (candidate == plans_.end()) throw std::length_error("Collection catalog cannot archive a terminal plan safely");
        // Preserve the entire original journal directory. The exclusive recorder
        // lock and broker mutex give this process sole mutation ownership.
        orderbook_jobs::private_dir(history_, false);
        const auto original = directory(candidate->first), archived = history_ / candidate->first;
        if (fs::exists(fs::symlink_status(archived))) throw std::runtime_error("Collection history identity already exists");
        const auto evidence = load(candidate->first);
        if (live(evidence.state)) throw std::runtime_error("Only terminal collection journals may be archived");
        fs::rename(original, archived);
        for (const auto& parent : {root_, history_}) {
            const int fd = ::open(parent.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
            if (fd < 0) throw std::runtime_error("Cannot flush collection history directory");
            const int result = ::fsync(fd); ::close(fd);
            if (result) throw std::runtime_error("Cannot flush collection history directory");
        }
        plans_.erase(candidate); --journal_count_;
    }
    bool cancel(Plan& p, ReadOnlyService& service, const std::string& reason, bool interrupted, Clock::time_point now) {
        elapsed(p, now);
        auto& s = p.segments.back(); s.reason = reason; s.state = "stopping"; p.state = "stopping";
        // A crash after this intent is never interpreted as a completed segment.
        try { persist(p); }
        catch (...) {
            if (service.depth() && service.depth()->request_id == s.request) service.disconnect();
            s.state = "interrupted"; s.gap = true; s.reason = "journal_failure";
            p.state = "interrupted"; p.reason = "journal_failure"; p.finished_ms = orderbook_jobs::now_ms(); active_.clear(); return false;
        }
        bool cancelled = false;
        try {
            if (service.depth() && service.depth()->request_id == s.request && service.generation() == generation_)
                cancelled = service.unsubscribe_depth(s.request);
        } catch (...) {
            // Unknown cancellation outcome cannot be retried into another segment.
            if (service.depth() && service.depth()->request_id == s.request) service.disconnect();
        }
        // ReadOnlyService clears its view even when the adapter cannot confirm
        // cancellation. Closing that connection bounds an unknown live request.
        if (!cancelled && service.generation() == generation_) service.disconnect();
        try { evidence(p, s); } catch (...) { cancelled = false; }
        const bool committed_stop = cancelled && s.archive_state == "stop" && s.ended_ms > 0 && s.sequence != "0" && s.event_count != "0" && s.raw_sequence == s.sequence;
        s.finalized = committed_stop && !interrupted;
        s.gap = interrupted || !committed_stop;
        s.state = s.finalized ? "stopped" : "interrupted";
        if (!committed_stop && !interrupted) s.reason = "stop_not_durable";
        if (!s.ended_ms) s.ended_ms = orderbook_jobs::now_ms();
        persist(p);
        return s.finalized;
    }
    void begin(Plan& p, ReadOnlyService& service, Clock::time_point now) {
        if (close_due(p, std::max(now, clock_()))) { finish(p, "stopped", "collection_window_closed"); return; }
        Segment s; s.index = static_cast<int>(p.segments.size()) + 1; s.started_ms = orderbook_jobs::now_ms();
        p.segments.push_back(s); p.state = "starting"; p.reason.clear();
        persist(p); // Subscribe intent precedes the broker call, including unknown outcomes.
        // Closing the preceding segment can drain callbacks and fsync both
        // archives. Its cost must not consume this segment's collection time.
        // The caller time also preserves deterministic injected-clock tests.
        const auto anchored = std::max(now, clock_());
        segment_started_ = anchored; next_health_ = anchored; last_tick_ = anchored;
        if (close_due(p, anchored)) {
            auto& pending = p.segments.back(); pending.state = "stopped"; pending.reason = "collection_window_closed";
            pending.ended_ms = wall_(); finish(p, "stopped", "collection_window_closed"); return;
        }
        try {
            p.segments.back().request = service.subscribe_depth(p.contract_id, p.venue, p.rows);
            evidence(p, p.segments.back());
            if (p.segments.back().archive_state != "recording") throw std::runtime_error("Missing recording acknowledgement");
            p.segments.back().state = "recording"; p.state = "recording"; persist(p);
        } catch (...) {
            if (service.depth() && service.depth()->request_id == p.segments.back().request) {
                try { service.unsubscribe_depth(p.segments.back().request); } catch (...) { service.disconnect(); }
            } else service.disconnect();
            auto& failed = p.segments.back(); failed.state = "interrupted"; failed.gap = true; failed.reason = "subscribe_failed";
            try { evidence(p, failed); } catch (...) {}
            if (!failed.ended_ms) failed.ended_ms = orderbook_jobs::now_ms();
            finish(p, "interrupted", "subscribe_failed");
        }
    }
    bool storage_ready() const {
        const auto state = store_.status();
        if (!state.open || state.failed) return false;
        struct statvfs disk{};
        return ::statvfs(root_.c_str(), &disk) == 0 &&
            static_cast<long double>(disk.f_bavail) * static_cast<long double>(disk.f_frsize) >= 2147483648.L;
    }
public:
    explicit Manager(storage::TimeSeriesStore& store, ClockSource clock = [] { return Clock::now(); },
                     WallClockSource wall = [] { return orderbook_jobs::now_ms(); })
        : store_(store), clock_(std::move(clock)), wall_(std::move(wall)),
          root_(fs::path(store.status().database).parent_path() / "depth-collections"),
          history_(root_.parent_path() / "depth-collection-history") {
        if (!clock_ || !wall_) throw std::invalid_argument("Collection requires steady and wall clock sources");
        orderbook_jobs::private_dir(root_, true);
        orderbook_jobs::private_dir(history_, true);
        for (const auto& entry : fs::directory_iterator(root_)) {
            const auto id = entry.path().filename().string();
            if (!orderbook_jobs::job_id(id)) { ++invalid_; continue; }
            if (++journal_count_ > plan_cap) { ++invalid_; continue; }
            try {
                auto p = load(id);
                if (live(p.state)) {
                    p.state = "interrupted"; p.reason = "server_restart"; p.finished_ms = orderbook_jobs::now_ms();
                    p.checkpoint_metrics_incomplete = true;
                    // The boundary-only journal can lag millions of committed
                    // callbacks. Refresh by the ORIGINAL saved session identity,
                    // never by a current-run/newest-session query or a DB write.
                    for (auto& s : p.segments) if (s.request && !s.session_id.empty()) {
                        try {
                            evidence(p, s, true);
                            if (s.finalized && (s.archive_state != "stop" || !s.ended_ms || s.sequence == "0" || s.raw_sequence != s.sequence))
                                throw std::runtime_error("Saved finalized evidence changed");
                        } catch (...) {
                            p.warnings.push_back("archive_evidence_unavailable"); s.finalized = false; s.gap = true;
                            s.state = "interrupted"; s.reason = "archive_evidence_unavailable";
                        }
                    }
                    if (!p.segments.empty() && live(p.segments.back().state)) {
                        auto& s = p.segments.back(); s.state = "interrupted"; s.reason = "server_restart"; s.gap = true; s.finalized = false;
                        if (!s.ended_ms) s.ended_ms = p.finished_ms;
                    }
                    persist(p);
                }
                if (current_.empty() || std::tie(plans_.at(current_).created_ms, plans_.at(current_).id) < std::tie(p.created_ms, p.id)) current_ = id;
                plans_.emplace(id, std::move(p));
            } catch (...) { ++invalid_; }
        }
    }
    bool active() const noexcept { return !active_.empty(); }
    const std::string& current_id() const noexcept { return current_; }
    Json status() const {
        Json out; out["schema_version"] = 1; out["active"] = active();
        out["current"] = current_.empty() ? Json(nullptr) : plan_json(plans_.at(current_));
        std::vector<const Plan*> sorted; for (const auto& [id, p] : plans_) sorted.push_back(&p);
        std::sort(sorted.begin(), sorted.end(), [](const auto* a, const auto* b) { return std::tie(a->created_ms, a->id) > std::tie(b->created_ms, b->id); });
        std::vector<Json> rows; for (std::size_t i = 0; i < std::min<std::size_t>(20, sorted.size()); ++i) rows.push_back(plan_json(*sorted[i]));
        out["plans"] = std::move(rows); out["catalog_limit"] = 20; out["plan_limit"] = 100;
        out["invalid_records"] = static_cast<std::uint64_t>(invalid_); out["automatic_reconnect"] = false; out["automatic_resume"] = false;
        out["terminal_journal_archival"] = true;
        out["timer_basis"] = "steady_clock";
        return out;
    }
    Json start(ReadOnlyService& service, const std::string& source, const Read& request, bool technical_checks_passed,
               const std::string& readiness_reason = "", Clock::time_point now = Clock::now()) {
        orderbook_jobs::exact_fields(request, {"schema_version", "contract_id", "venue", "rows", "segment_seconds", "segments", "paper_read_only_confirmed"});
        orderbook_jobs::bounded(request, "schema_version", 1, 1);
        if (!boolean(request, "paper_read_only_confirmed")) throw std::invalid_argument("Confirm the intended paper session and Read-Only API before collection");
        const auto cid = orderbook_jobs::field(request, "contract_id"), venue = orderbook_jobs::field(request, "venue");
        if (!decimal(cid, false) || (venue != "BATS" && venue != "IEX")) throw std::invalid_argument("Choose a resolved direct BATS or IEX equity");
        ContractId contract = 0;
        try { contract = std::stoll(cid); }
        catch (const std::exception&) { throw std::invalid_argument("Contract ID is outside supported bounds"); }
        const int rows = orderbook_jobs::bounded(request, "rows", 1, 50), seconds = orderbook_jobs::bounded(request, "segment_seconds", 60, 3600), count = orderbook_jobs::bounded(request, "segments", 1, 16);
        if (seconds * count > 14400) throw std::invalid_argument("Collection is limited to four hours per explicit plan");
        if (source != "ibkr_tws" && source != "mock") throw std::invalid_argument("Unsupported depth recorder source");
        if (active() || service.depth()) throw std::logic_error("Stop the previous collection or depth request first");
        if (service.state() != ConnectionState::Ready) throw std::logic_error("Connect the confirmed paper session first");
        if (!technical_checks_passed) throw std::logic_error("Collection readiness checks failed: " + code(readiness_reason, "preflight_failed"));
        if (!storage_ready()) throw std::runtime_error("Recorder unhealthy or less than 2 GiB free space");
        DepthSpec spec{service.contract(contract), venue, rows}; spec.validate();
        make_room();
        Plan p; p.id = local_auth::random_secret().substr(0, 32); p.source = source; p.contract_id = contract;
        p.recorder_run_id = store_.status().run_id;
        p.symbol = spec.contract.symbol; p.currency = spec.contract.currency; p.venue = venue; p.rows = rows; p.seconds = seconds; p.count = count;
        p.created_ms = orderbook_jobs::now_ms(); p.updated_ms = p.created_ms;
        orderbook_jobs::private_dir(root_ / p.id, true); ++journal_count_;
        const auto id = p.id; plans_.emplace(id, std::move(p)); current_ = id; active_ = id; generation_ = service.generation();
        try { begin(plans_.at(id), service, now); }
        catch (...) { interrupt(service, "journal_failure", now); throw std::runtime_error("Cannot persist the collection start"); }
        return status();
    }
    Json scheduled_start(ReadOnlyService& service, const std::string& source, const Read& request,
                         bool technical_checks_passed, const std::string& readiness_reason = "",
                         Clock::time_point now = Clock::now()) {
        orderbook_jobs::exact_fields(request, {"schema_version", "schedule_id", "collection_id", "session_date", "open_ms", "close_ms",
            "contract_id", "venue", "rows", "paper_read_only_confirmed", "expected_account"});
        orderbook_jobs::bounded(request, "schema_version", 1, 1);
        const auto schedule = orderbook_jobs::field(request, "schedule_id"), id = orderbook_jobs::field(request, "collection_id"),
            day = orderbook_jobs::field(request, "session_date"), account = orderbook_jobs::field(request, "expected_account");
        if (source != "ibkr_tws" || !orderbook_jobs::job_id(schedule) || !orderbook_jobs::job_id(id) || !iso_date(day) || !paper_account(account) ||
            !boolean(request, "paper_read_only_confirmed") || orderbook_jobs::field(request, "contract_id") != "756733" ||
            orderbook_jobs::field(request, "venue") != "BATS" || orderbook_jobs::bounded(request, "rows", 50, 50) != 50)
            throw std::invalid_argument("Scheduled collection requires the configured paper SPY/BATS scope");
        std::int64_t open = 0, close = 0;
        try { open = timestamp(request, "open_ms"); close = timestamp(request, "close_ms"); }
        catch (const std::exception&) { throw std::invalid_argument("Scheduled market bounds must be integer Unix milliseconds"); }
        if (close <= open || close - open > 27000000) throw std::invalid_argument("Scheduled collection window exceeds seven and a half hours");
        const auto response = [&](const Plan& p) {
            auto out = status(); out["requested_plan"] = plan_json(p); return out;
        };
        const auto same_intent = [&](const Plan& p) {
            if (p.schedule_id != schedule || p.session_date != day || p.collection_open_ms != open || p.collection_close_ms != close ||
                p.contract_id != 756733 || p.venue != "BATS" || p.rows != 50 || p.source != source)
                throw std::logic_error("Collection identity already belongs to another immutable intent");
            if (active_ == id && expected_account_ != account) throw std::logic_error("Scheduled collection account binding changed");
        };
        if (const auto existing = plans_.find(id); existing != plans_.end()) {
            same_intent(existing->second); return response(existing->second);
        }
        if (fs::exists(fs::symlink_status(history_ / id))) {
            const auto archived = load(id, true); same_intent(archived); return response(archived);
        }
        const auto steady_anchor = std::max(now, clock_());
        const auto wall_now = wall_();
        if (wall_now < open || wall_now >= close) throw std::logic_error("Scheduled collection is outside its authorized collection window");
        if (active() || service.depth()) throw std::logic_error("An existing manual or scheduled collection owns the depth request");
        if (service.state() != ConnectionState::Ready || !account_matches(service, account))
            throw std::logic_error("Fresh managed accounts do not match the configured paper account");
        if (!technical_checks_passed) throw std::logic_error("Collection readiness checks failed: " + code(readiness_reason, "preflight_failed"));
        if (!storage_ready()) throw std::runtime_error("Recorder unhealthy or less than 2 GiB free space");
        const auto contract = service.contract(756733);
        if (contract.symbol != "SPY" || contract.currency != "USD" || contract.security_type != SecurityType::Equity)
            throw std::logic_error("Resolve the fixed SPY USD stock contract before scheduled collection");
        DepthSpec{contract, "BATS", 50}.validate(); make_room();
        Plan p; p.id = id; p.source = source; p.contract_id = 756733; p.recorder_run_id = store_.status().run_id;
        p.symbol = "SPY"; p.currency = "USD"; p.venue = "BATS"; p.rows = 50; p.seconds = 900;
        p.count = static_cast<int>((close - wall_now + 899999) / 900000);
        p.schedule_id = schedule; p.session_date = day; p.collection_open_ms = open; p.collection_close_ms = close;
        p.created_ms = orderbook_jobs::now_ms(); p.updated_ms = p.created_ms;
        // Both clocks may end this run; moving wall time backwards cannot extend it.
        collection_close_deadline_ = steady_anchor + std::chrono::milliseconds(close - wall_now);
        orderbook_jobs::private_dir(root_ / id, true); ++journal_count_;
        plans_.emplace(id, std::move(p)); current_ = id; active_ = id; generation_ = service.generation(); expected_account_ = account;
        try { begin(plans_.at(id), service, now); }
        catch (...) { interrupt(service, "journal_failure", now); throw std::runtime_error("Cannot persist scheduled collection start"); }
        return response(plans_.at(id));
    }
    void tick(ReadOnlyService& service, const std::string& source, bool technical_checks_passed,
              const std::string& readiness_reason = "", Clock::time_point now = Clock::now()) {
        if (!active()) return;
        auto& p = plans_.at(active_);
        const auto halt = [&](const std::string& reason) { interrupt(service, reason, now); };
        // These cheap guards precede throttling. A failed clock observation or
        // backward steady clock must never postpone its own interruption.
        if (now < segment_started_ || now < last_tick_) { halt("steady_clock_regression"); return; }
        last_tick_ = now; elapsed(p, now);
        if (close_due(p, now)) { stop(service, "collection_window_closed", now); return; }
        if (source != p.source || service.generation() != generation_) { halt("connection_generation_changed"); return; }
        if (service.state() != ConnectionState::Ready) { halt("broker_disconnected"); return; }
        if (!p.schedule_id.empty() && !account_matches(service, expected_account_)) { halt("paper_account_binding_changed"); return; }
        if (!technical_checks_passed) { halt(code(readiness_reason, "preflight_failed")); return; }
        if (now < next_health_ && p.segments.back().elapsed < p.seconds) return;
        next_health_ = now + std::chrono::seconds(1);
        auto& s = p.segments.back();
        if (!storage_ready()) { halt("recorder_or_disk_failure"); return; }
        if (!service.depth() || service.depth()->request_id != s.request || service.depth()->spec.contract.id != p.contract_id || service.depth()->spec.venue != p.venue || service.depth()->spec.rows != p.rows) { halt("depth_request_changed"); return; }
        const auto& depth = *service.depth(); s.updates = depth.update_event_count;
        p.warnings.clear(); p.quality = depth.book.quality(); p.update_age.reset();
        if (depth.book.empty_tail_delete_count()) p.warnings.push_back("empty_tail_delete_callback");
        if (depth.last_update) p.update_age = std::max(0., static_cast<double>(std::chrono::duration_cast<std::chrono::nanoseconds>(now.time_since_epoch()).count() - depth.last_update->monotonic_ns) / 1e9);
        if (!s.updates) p.warnings.push_back("awaiting_depth_delivery");
        else if (p.update_age && *p.update_age > 15) p.warnings.push_back("no_recent_depth_updates");
        if (depth.book.sequence() && (!depth.book.structural_valid() || !depth.book.active())) { halt("structural_depth_failure"); return; }
        if (p.quality != "two_sided_unverified" && p.quality != "not_started" && p.quality != "one_sided_or_building") p.warnings.push_back("book_" + p.quality);
        try { evidence(p, s); } catch (...) { halt("recorder_evidence_failure"); return; }
        if (s.raw_sequence.empty() || s.raw_sequence != s.sequence) { halt("recorder_evidence_failure"); return; }
        if (s.archive_state != "recording") { halt("archive_not_recording"); return; }
        if (s.elapsed < p.seconds) return;
        try {
            if (!cancel(p, service, "timer_elapsed", false, now)) { finish(p, "interrupted", "stop_not_durable"); return; }
            if (close_due(p, std::max(now, clock_()))) { finish(p, "stopped", "collection_window_closed"); return; }
            if (static_cast<int>(p.segments.size()) == p.count) { finish(p, "completed", "timer_elapsed"); return; }
            if (service.state() != ConnectionState::Ready || service.generation() != generation_ || !technical_checks_passed || !storage_ready()) { finish(p, "interrupted", "boundary_preflight_failed"); return; }
            begin(p, service, now);
        } catch (...) { interrupt(service, "journal_or_boundary_failure", now); }
    }
    Json stop(ReadOnlyService& service, const std::string& reason = "user_stop", Clock::time_point now = Clock::now()) {
        if (active()) {
            auto& p = plans_.at(active_); const auto safe_reason = code(reason, "user_stop");
            try {
                if (cancel(p, service, safe_reason, false, now)) finish(p, "stopped", safe_reason);
                else finish(p, "interrupted", "stop_not_durable");
            } catch (...) { interrupt(service, "journal_failure", now); }
        }
        return status();
    }
    void interrupt(ReadOnlyService& service, const std::string& reason, Clock::time_point now = Clock::now()) noexcept {
        if (!active()) return;
        const auto id = active_; const auto safe_reason = code(reason, "collection_interrupted");
        try {
            auto& p = plans_.at(id);
            cancel(p, service, safe_reason, true, now);
            finish(p, "interrupted", safe_reason);
        } catch (...) {
            // Leave the prior fsynced start/stop intent for restart recovery.
            auto& p = plans_.at(id); p.state = "interrupted"; p.reason = safe_reason; p.finished_ms = orderbook_jobs::now_ms();
            if (!p.segments.empty()) { auto& s = p.segments.back(); s.state = "interrupted"; s.gap = true; s.finalized = false; s.reason = safe_reason; }
            active_.clear();
            if (service.depth() && !p.segments.empty() && service.depth()->request_id == p.segments.back().request) service.disconnect();
            try { persist(p); } catch (...) {}
        }
    }
};
} // namespace dts::collection_http
