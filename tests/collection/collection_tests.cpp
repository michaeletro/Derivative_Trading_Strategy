#define CROW_USE_BOOST 1
#include <boost/asio.hpp>
namespace asio = boost::asio;
#include "crow_all.h"
#include "depth_collection.hpp"
#include <dts/recording_broker.hpp>
#include <dts/tws_state.hpp>
#include <functional>
#include <iostream>

namespace {
using namespace dts;
using namespace dts::collection_http;
using orderbook_jobs::field;
using orderbook_jobs::read_json;
int passed = 0, serial = 0, unexpected_store_failures = 0;
void check(bool value, const char* label) { if (!value) throw std::runtime_error(label); }
template<class F> void rejects(F f) { bool rejected = false; try { f(); } catch (const std::exception&) { rejected = true; } check(rejected, "Expected rejection"); }
Read parsed(Json value) { return crow::json::load(value.dump()); }
Read request(int seconds = 60, int count = 2) {
    auto j = crow::json::load(R"JSON({"schema_version":1,"contract_id":"9001","venue":"BATS","rows":30,"segment_seconds":60,"segments":2,"paper_read_only_confirmed":true})JSON");
    Json out(j); out["segment_seconds"] = seconds; out["segments"] = count;
    return parsed(std::move(out));
}
Contract instrument() { Contract c; c.id = 9001; c.symbol = "SYNTHETIC"; c.currency = "USD"; c.exchange = "SMART"; c.multiplier = 1; return c; }
class Peer final : public IBroker {
public:
    TwsState state_;
    bool spy = false;
    std::string symbol_override;
    int subscriptions = 0, cancellations = 0, disconnections = 0;
    bool unknown_cancel = false, unknown_subscribe = false;
    RequestId last = 0;
    std::vector<DepthSpec> requested;
    std::function<void(const char*)> before;
    std::function<Clock::time_point()> request_clock = [] { return Clock::now(); };
    void connect() override { state_.start(Clock::now()); state_.ready(); state_.managed_accounts({true,{"DU1234567"}}); }
    void disconnect() noexcept override { ++disconnections; state_.disconnect(); }
    ConnectionState state() const noexcept override { return state_.state(); }
    RequestId resolve(const ContractQuery& q) override {
        auto id = state_.resolve(q, Clock::now()); auto contract = instrument();
        if (spy) { contract.id = 756733; contract.symbol = "SPY"; }
        if (!symbol_override.empty()) contract.symbol = symbol_override;
        state_.contract(id, contract); state_.contract_end(id); return id;
    }
    RequestId subscribe(const Contract&) override { return 0; }
    bool unsubscribe(RequestId) override { return false; }
    void request_positions(RequestId) override {}
    RequestId subscribe_depth(const DepthSpec& spec) override {
        if (before) before("subscribe");
        ++subscriptions; requested.push_back(spec); last = state_.depth(spec, request_clock());
        if (unknown_subscribe) throw std::runtime_error("Synthetic unknown subscribe result");
        return last;
    }
    bool unsubscribe_depth(RequestId id) override {
        if (before) before("cancel");
        ++cancellations; if (unknown_cancel) return false;
        return state_.cancel_depth(id);
    }
    std::vector<BrokerEvent> poll() override { return state_.poll(); }
    void update(int side, int position, int operation, double price) {
        DepthEvent e; e.request_id = last; e.kind = "update"; e.side = side; e.position = position; e.operation = operation;
        e.price = price; e.size = "100"; state_.depth_update(e);
    }
};
struct Fixture {
    fs::path root = fs::temp_directory_path() / ("dts-collection-" + std::to_string(getpid()) + "-" + std::to_string(++serial));
    storage::TimeSeriesStore store;
    Peer* peer = nullptr;
    std::unique_ptr<ReadOnlyService> service;
    bool expected_store_failure = false;
    Fixture(const std::string& source = "mock", bool spy = false, const std::string& symbol_override = "") : store(storage::Config{root, root / "backups"}, source) {
        auto inner = std::make_unique<Peer>(); peer = inner.get();
        peer->spy = spy; peer->symbol_override = symbol_override;
        service = std::make_unique<ReadOnlyService>(std::make_unique<storage::RecordingBroker>(std::move(inner), store, source));
        service->connect(); service->poll(); ContractQuery q; q.symbol = spy ? "SPY" : "SYNTHETIC"; service->resolve(q); service->poll();
    }
    ~Fixture() {
        service->disconnect();
        if (store.status().failed && !expected_store_failure) ++unexpected_store_failures;
        try { store.close(); } catch (const std::exception&) { if (!expected_store_failure) ++unexpected_store_failures; }
        fs::remove_all(root);
    }
    void delivered() { peer->update(0, 0, 0, 101); peer->update(1, 0, 0, 99); service->poll(); }
};
constexpr std::int64_t collection_open = 1792069200000;
Read scheduled_request(std::int64_t open = collection_open, std::int64_t close = collection_open + 27000000,
                       const std::string& id = "11111111111111111111111111111111") {
    Json out;
    out["schema_version"] = 1; out["schedule_id"] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"; out["collection_id"] = id;
    out["session_date"] = "2026-10-15"; out["open_ms"] = open; out["close_ms"] = close;
    out["contract_id"] = "756733"; out["venue"] = "BATS"; out["rows"] = 50;
    out["paper_read_only_confirmed"] = true; out["expected_account"] = "DU1234567";
    return parsed(std::move(out));
}
template<class F> void test(const char* name, F f) {
    f(); check(!unexpected_store_failures, "Unexpected recorder failure during fixture teardown");
    ++passed; std::cout << "PASS " << name << '\n';
}
}

int main() {
    try {
        test("exact bounded request and explicit readiness", [] {
            Fixture f; Manager m(f.store);
            auto bad = Json(request()); bad["paper_read_only_confirmed"] = false;
            rejects([&] { m.start(*f.service, "mock", parsed(std::move(bad)), true); });
            rejects([&] { m.start(*f.service, "mock", request(), false, "clock_checking"); });
            rejects([&] { m.start(*f.service, "mock", request(59), true); });
            rejects([&] { m.start(*f.service, "mock", request(3600, 5), true); });
            rejects([&] { m.start(*f.service, "mock", request(60, 17), true); });
            for (const auto* key : {"rows", "segments", "segment_seconds", "schema_version"}) {
                auto altered = Json(request()); altered[key] = true;
                rejects([&] { m.start(*f.service, "mock", parsed(std::move(altered)), true); });
            }
            bad = Json(request()); bad["venue"] = "SMART"; rejects([&] { m.start(*f.service, "mock", parsed(std::move(bad)), true); });
            bad = Json(request()); bad["contract_id"] = "756733"; rejects([&] { m.start(*f.service, "mock", parsed(std::move(bad)), true); });
            bad = Json(request()); bad["contract_id"] = "9999999999999999999";
            bool invalid_id = false;
            try { m.start(*f.service, "mock", parsed(std::move(bad)), true); }
            catch (const std::invalid_argument&) { invalid_id = true; }
            check(invalid_id, "Oversized contract IDs are invalid requests, not archive lookup errors");
            bad = Json(request()); bad["auto_connect"] = true; rejects([&] { m.start(*f.service, "mock", parsed(std::move(bad)), true); });
            check(f.peer->subscriptions == 0, "Rejected plans cannot subscribe");
        });
        test("steady timers finalize each segment before the next same-instrument request", [] {
            Fixture f; Manager m(f.store); const auto start = Clock::now() + std::chrono::seconds(100);
            f.peer->before = [&](const char* action) {
                const auto root = f.root / "depth-collections";
                if (std::string(action) == "subscribe") {
                    for (const auto& entry : fs::directory_iterator(root)) {
                        const auto intent = read_json(entry.path() / "state.json", 131072);
                        check(field(intent, "state") == "starting", "Subscribe intent must be durable first");
                        const auto& segments = intent["segments"];
                        if (segments.size() == 2) check(segments[0]["finalized"].b() && field(segments[0], "archive_state") == "stop", "Previous segment must have durable stop evidence");
                    }
                } else {
                    auto intent = read_json(root / m.current_id() / "state.json", 131072);
                    check(field(intent, "state") == "stopping", "Cancel intent must be durable first");
                }
            };
            auto result = parsed(m.start(*f.service, "mock", request(), true, "", start));
            check(result["active"].b() && field(result["current"], "source") == "mock", "Explicit synthetic source");
            rejects([&] { m.start(*f.service, "mock", request(), true); });
            f.delivered(); m.tick(*f.service, "mock", true, "", start + std::chrono::seconds(59));
            check(f.peer->subscriptions == 1 && f.peer->cancellations == 0, "No early wall-clock-based rotation");
            m.tick(*f.service, "mock", true, "", start + std::chrono::seconds(60));
            result = parsed(m.status());
            check(f.peer->subscriptions == 2 && f.peer->cancellations == 1 && result["current"]["segments_completed"].i() == 1, "First segment durable before second subscription");
            for (const auto& spec : f.peer->requested) check(spec.contract.id == 9001 && spec.venue == "BATS" && spec.rows == 30, "Instrument settings frozen across rotations");
            f.delivered(); m.tick(*f.service, "mock", true, "", start + std::chrono::seconds(120));
            result = parsed(m.status());
            check(!m.active() && !f.service->depth() && field(result["current"], "state") == "completed", "Finite plan ends without another request");
            check(!result["current"]["checkpoint_metrics_incomplete"].b(), "Fresh completion retains complete observed metrics");
            check(result["current"]["segments_completed"].i() == 2 && result["current"]["elapsed_seconds"].d() >= 120, "Both scheduled segments accounted");
            for (const auto& s : result["current"]["segments"]) {
                check(s["finalized"].b() && !s["gap"].b() && s["delivery_observed"].b(), "Stop and delivery are distinct evidence");
                check(field(s, "raw_committed_sequence") == field(s, "committed_sequence"), "Raw watermark covers final stop");
                check(field(s, "reason") == "timer_elapsed", "Explicit scheduled stop reason");
            }
            m.tick(*f.service, "mock", true, "", start + std::chrono::hours(1));
            check(f.peer->subscriptions == 2, "Completed plans never restart themselves");
            Manager reopened(f.store); const auto saved = parsed(reopened.status());
            check(!reopened.active() && field(saved["current"], "state") == "completed", "Completed evidence survives restart");
        });
        test("early user stop is durable but not scheduled completion", [] {
            Fixture f; Manager m(f.store); const auto start = Clock::now() + std::chrono::seconds(100);
            m.start(*f.service, "mock", request(), true, "", start); f.delivered();
            m.tick(*f.service, "mock", true, "", start + std::chrono::seconds(10));
            auto result = parsed(m.stop(*f.service, "user_stop", start + std::chrono::seconds(12)));
            check(field(result["current"], "state") == "stopped" && field(result["current"], "reason") == "user_stop", "Manual stop is explicit");
            check(result["current"]["segments"].size() == 1 && result["current"]["segments"][0]["finalized"].b(), "Partial-duration recording still has durable stop");
            check(result["current"]["elapsed_seconds"].d() == 12 && f.peer->subscriptions == 1, "No replacement segment after manual stop");
        });
        test("prior segment drain time cannot consume the next segment duration", [] {
            Fixture f; const auto start = Clock::now() + std::chrono::seconds(100);
            auto fresh = start; Manager m(f.store, [&] { return fresh; });
            m.start(*f.service, "mock", request(), true, "", start); f.delivered();
            f.peer->before = [&](const char* action) {
                if (std::string(action) == "cancel" && f.peer->cancellations == 0)
                    fresh = start + std::chrono::seconds(65); // Five seconds of drain/fsync work.
            };
            m.tick(*f.service, "mock", true, "", start + std::chrono::seconds(60));
            check(m.active() && f.peer->subscriptions == 2, "Next segment starts after prior close");
            f.delivered();
            m.tick(*f.service, "mock", true, "", start + std::chrono::seconds(120));
            check(m.active() && f.peer->cancellations == 1, "Second segment has only 55 elapsed seconds after slow first close");
            const auto snapshot = parsed(m.status());
            check(snapshot["current"]["segments"][1]["elapsed_seconds"].d() == 55, "Only new collection duration is attributed to the next segment");
            m.tick(*f.service, "mock", true, "", start + std::chrono::seconds(125));
            check(!m.active() && f.peer->cancellations == 2, "Second segment still receives its full scheduled minute");
        });
        test("restart interrupts durable active intent without altering the archive", [] {
            Fixture f; Manager original(f.store); original.start(*f.service, "mock", request(), true); f.delivered();
            const auto sid = field(parsed(original.status())["current"]["segments"][0], "session_id");
            const auto saved = read_json(f.root / "depth-collections" / original.current_id() / "state.json", 131072);
            check(field(saved["segments"][0], "committed_event_count") == "0", "Boundary-only journal intentionally lags live callbacks");
            Manager reopened(f.store); auto result = parsed(reopened.status());
            check(!reopened.active() && field(result["current"], "state") == "interrupted" && field(result["current"], "reason") == "server_restart", "No automatic resume");
            check(result["current"]["checkpoint_metrics_incomplete"].b(), "Restart labels elapsed and observed updates as checkpoint lower bounds");
            check(result["current"]["segments"][0]["gap"].b() && !result["current"]["segments"][0]["finalized"].b(), "Unknown prior outcome remains a gap");
            check(std::stoll(field(result["current"]["segments"][0], "committed_event_count")) >= 3, "Restart refreshes actual saved event counts by original session identity");
            check(field(result["current"]["segments"][0], "raw_committed_sequence") == field(result["current"]["segments"][0], "committed_sequence"), "Original raw watermark refreshed without current-run lookup");
            check(std::get<std::string>(f.store.depth_session(std::stoll(sid)).at("state")) == "recording", "Manager does not rewrite archive recovery state");
            reopened.tick(*f.service, "mock", true); check(f.peer->subscriptions == 1, "Recovered journal cannot subscribe");
            original.stop(*f.service);
        });
        test("process crash recovers original-run counts and preserves interrupted archive state", [] {
            const auto root = fs::temp_directory_path() / ("dts-collection-crash-" + std::to_string(getpid()) + "-" + std::to_string(++serial));
            const auto child = ::fork(); check(child >= 0, "Fork synthetic crash recorder");
            if (child == 0) {
                try {
                    storage::TimeSeriesStore store({root, root / "backups"}, "mock");
                    auto peer = std::make_unique<Peer>(); auto* raw = peer.get();
                    ReadOnlyService service(std::make_unique<storage::RecordingBroker>(std::move(peer), store, "mock"));
                    service.connect(); service.poll(); ContractQuery q; q.symbol = "SYNTHETIC"; service.resolve(q); service.poll();
                    Manager m(store); m.start(service, "mock", request(), true);
                    for (int row = 0; row < 30; ++row) {
                        raw->update(0, row, 0, 101 + row); raw->update(1, row, 0, 99 - row);
                    }
                    service.poll();
                    check(std::stoll(std::get<std::string>(store.depth_recording("mock", raw->last).at("event_count"))) > 60, "Synthetic callbacks committed before crash");
                    std::_Exit(0); // Deliberately no manager stop or recorder shutdown.
                } catch (...) { std::_Exit(1); }
            }
            int child_status = 0;
            check(::waitpid(child, &child_status, 0) == child && WIFEXITED(child_status) && WEXITSTATUS(child_status) == 0, "Synthetic recorder reached deliberate crash boundary");
            {
                storage::TimeSeriesStore store({root, root / "backups"}, "mock");
                Manager reopened(store); const auto result = parsed(reopened.status());
                const auto& p = result["current"]; const auto& s = p["segments"][0];
                check(!reopened.active() && field(p, "reason") == "server_restart" && p["checkpoint_metrics_incomplete"].b(), "Original plan recovers without resume");
                check(std::stoll(field(s, "committed_event_count")) > 60 && field(s, "archive_state") == "interrupted", "Startup's actual interrupted state and committed count are retained");
                check(s["gap"].b() && !s["finalized"].b(), "Recovery marker cannot qualify a completed segment");
                check(std::stoll(field(s, "raw_committed_sequence")) + 1 == std::stoll(field(s, "committed_sequence")), "DB-only recorder recovery marker does not promote raw tail");
                check(!store.status().failed, "Crash recovery remains healthy for later explicit collection");
                store.close();
            }
            fs::remove_all(root);
        });
        test("transient book shape warnings preserve collection but structural faults stop it", [] {
            Fixture f; Manager m(f.store); const auto start = Clock::now() + std::chrono::seconds(100);
            m.start(*f.service, "mock", request(), true, "", start); f.delivered();
            f.peer->update(1, 0, 1, 102); f.service->poll();
            m.tick(*f.service, "mock", true, "", start + std::chrono::seconds(1));
            auto result = parsed(m.status());
            check(m.active() && result["current"]["warnings"].size() > 0 && field(result["current"], "book_quality") == "crossed", "Crossed shape is a warning, not automatic resubscribe");
            f.peer->update(0, 4, 1, 103); f.service->poll();
            m.tick(*f.service, "mock", true, "", start + std::chrono::seconds(2)); result = parsed(m.status());
            check(!m.active() && field(result["current"], "reason") == "structural_depth_failure", "Missing row is a structural interruption");
            check(!result["current"]["segments"][0]["finalized"].b() && result["current"]["segments"][0]["gap"].b(), "Explicit stop cannot erase structural failure");
            check(f.peer->subscriptions == 1, "Structural fault never resubscribes");
        });
        test("technical clock failure stops without retries or fabricated finalization", [] {
            Fixture f; Manager m(f.store); const auto start = Clock::now() + std::chrono::seconds(100);
            m.start(*f.service, "mock", request(), true, "", start); f.delivered();
            m.tick(*f.service, "mock", true, "", start);
            m.tick(*f.service, "mock", false, "clock_drift", start + std::chrono::milliseconds(500));
            const auto result = parsed(m.status());
            check(!m.active() && field(result["current"], "reason") == "clock_drift", "Technical guard reason preserved");
            check(result["current"]["segments"][0]["gap"].b() && f.peer->subscriptions == 1, "Failure is terminal and conservative");
        });
        test("received empty tail cleanup cannot abort a timed collection", [] {
            Fixture f;Manager m(f.store);const auto start=Clock::now()+std::chrono::seconds(100);
            m.start(*f.service,"mock",request(),true,"",start);
            for(int pos=0;pos<10;++pos)f.peer->update(1,pos,0,99.-pos);
            f.peer->update(0,0,0,101);
            for(int n=0;n<254;++n)f.peer->update(1,0,1,99); // Start+11 inserts+254 updates: sequence 266.
            f.peer->update(1,8,2,0);f.peer->update(1,9,2,0);f.peer->update(1,0,1,99);
            f.service->poll();m.tick(*f.service,"mock",true,"",start+std::chrono::seconds(1));
            const auto observed=parsed(m.status());
            check(m.active()&&f.service->depth()->book.sequence()==269&&f.service->depth()->book.empty_tail_delete_count()==1,"Actual ascending tail cleanup pattern remains structurally usable");
            bool warned=false;for(const auto& warning:observed["current"]["warnings"])if(std::string(warning.s())=="empty_tail_delete_callback")warned=true;
            check(warned,"Ignored empty-tail callback remains explicit in collection warnings");
            m.stop(*f.service);const auto stopped=parsed(m.status())["current"]["segments"][0];
            check(stopped["finalized"].b()&&field(stopped,"committed_sequence")=="270"&&field(stopped,"raw_committed_sequence")=="270", "All received callbacks and final stop retained in archive and raw metadata");
        });
        test("steady regression interrupts before the next throttled health slot", [] {
            Fixture f; Manager m(f.store); const auto start = Clock::now() + std::chrono::seconds(100);
            m.start(*f.service, "mock", request(), true, "", start); f.delivered();
            m.tick(*f.service, "mock", true, "", start);
            m.tick(*f.service, "mock", true, "", start + std::chrono::milliseconds(500));
            m.tick(*f.service, "mock", true, "", start + std::chrono::milliseconds(250));
            const auto result = parsed(m.status());
            check(!m.active() && field(result["current"], "reason") == "steady_clock_regression", "Backward steady clock cannot defer its own interruption");
            check(result["current"]["elapsed_seconds"].d() == .5, "Regression does not invent elapsed capture time");
            check(f.peer->subscriptions == 1, "No automatic subscription after clock regression");
        });
        test("connection loss halts the plan with archived outcome visible", [] {
            Fixture f; Manager m(f.store); const auto start = Clock::now() + std::chrono::seconds(100);
            m.start(*f.service, "mock", request(), true, "", start); f.delivered();
            f.service->disconnect(); m.tick(*f.service, "mock", true, "", start + std::chrono::seconds(1));
            const auto result = parsed(m.status());
            check(!m.active() && field(result["current"], "state") == "interrupted" && result["current"]["segments"][0]["gap"].b(), "Disconnect interrupts regardless of orderly adapter close");
            check(f.peer->subscriptions == 1 && f.service->state() == ConnectionState::Disconnected, "No automatic reconnect");
        });
        test("unknown cancellation closes the connection and prevents the next segment", [] {
            Fixture f; Manager m(f.store); const auto start = Clock::now() + std::chrono::seconds(100);
            m.start(*f.service, "mock", request(), true, "", start); f.delivered(); f.peer->unknown_cancel = true;
            m.tick(*f.service, "mock", true, "", start + std::chrono::seconds(60));
            const auto result = parsed(m.status());
            check(!m.active() && field(result["current"], "state") == "interrupted" && field(result["current"], "reason") == "stop_not_durable", "Unknown cancel is never success");
            check(f.peer->subscriptions == 1 && f.service->state() == ConnectionState::Disconnected, "Connection closed to bound unknown request");
            check(!result["current"]["segments"][0]["finalized"].b(), "Later recorder stop does not prove original cancellation");
        });
        test("unknown subscribe cannot leave a hidden active request or auto retry", [] {
            Fixture f; Manager m(f.store); f.peer->unknown_subscribe = true; f.expected_store_failure = true;
            const auto result = parsed(m.start(*f.service, "mock", request(), true));
            check(!m.active() && field(result["current"], "reason") == "subscribe_failed", "Unknown subscribe terminal outcome");
            check(f.service->state() == ConnectionState::Disconnected && f.peer->subscriptions == 1, "Unknown native request bounded by disconnect");
            check(f.store.status().failed, "Unregistered unknown callback is retained as a sticky recorder failure");
            Manager reopened(f.store); check(!reopened.active(), "Failed subscription never resumes");
        });
        test("missing raw metadata interrupts collection and retains recorder failure", [] {
            Fixture f; Manager m(f.store); const auto start = Clock::now() + std::chrono::seconds(100);
            m.start(*f.service, "mock", request(), true, "", start); f.delivered();
            const auto sid = field(parsed(m.status())["current"]["segments"][0], "session_id");
            const auto metadata = f.store.depth_raw_metadata(std::stoll(sid));
            const auto path = fs::path(std::get<std::string>(metadata.at("path")));
            fs::rename(path, path.string() + ".preserved"); f.expected_store_failure = true;
            m.tick(*f.service, "mock", true, "", start + std::chrono::seconds(1));
            const auto result = parsed(m.status());
            check(!m.active() && field(result["current"], "reason") == "recorder_evidence_failure", "Lost sidecar is not silently ignored until rotation");
            check(result["current"]["segments"][0]["gap"].b() && !result["current"]["segments"][0]["finalized"].b(), "Missing metadata cannot qualify a segment");
            check(f.store.status().failed && f.peer->subscriptions == 1, "Recorder failure is sticky and no retry occurs");
            fs::rename(path.string() + ".preserved", path);
        });
        test("private journal refuses symlink paths and preserves foreign files", [] {
            Fixture f;
            const auto linked = f.root / "depth-collections";
            fs::create_directory_symlink(f.root / "backups", linked);
            rejects([&] { Manager m(f.store); }); fs::remove(linked);
            Manager m(f.store); m.start(*f.service, "mock", request(), true); m.stop(*f.service);
            const auto file = f.root / "depth-collections" / m.current_id() / "state.json";
            struct stat st{}; check(!::lstat(file.c_str(), &st) && (st.st_mode & 0777) == 0600, "Private journal files mode 0600");
            check(!::lstat(file.parent_path().c_str(), &st) && (st.st_mode & 0777) == 0700, "Private plan directory mode 0700");
            const auto raw = orderbook_jobs::read_file(file, 131072);
            check(raw.find("token") == std::string::npos && raw.find("account") == std::string::npos && raw.find("password") == std::string::npos, "Journal has no account or credential fields");
        });
        test("scheduled fixed scope paper account and native source fail closed", [] {
            Fixture f("ibkr_tws", true); auto wall = collection_open; const auto start = Clock::now();
            Manager m(f.store, [&] { return start; }, [&] { return wall; });
            rejects([&] { m.scheduled_start(*f.service, "mock", scheduled_request(), true, "", start); });
            rejects([&] { m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), false, "clock_checking", start); });
            rejects([&] { m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(collection_open, collection_open + 27000001), true, "", start); });
            for (const auto* key : {"contract_id", "venue", "expected_account", "session_date"}) {
                auto bad = Json(scheduled_request()); bad[key] = "invalid";
                rejects([&] { m.scheduled_start(*f.service, "ibkr_tws", parsed(std::move(bad)), true, "", start); });
            }
            auto bad = Json(scheduled_request()); bad["paper_read_only_confirmed"] = false;
            rejects([&] { m.scheduled_start(*f.service, "ibkr_tws", parsed(std::move(bad)), true, "", start); });
            for (const auto* key : {"rows", "open_ms", "close_ms"}) {
                bad = Json(scheduled_request()); bad[key] = true;
                rejects([&] { m.scheduled_start(*f.service, "ibkr_tws", parsed(std::move(bad)), true, "", start); });
            }
            bad = Json(scheduled_request()); bad["session_date"] = "2026-02-30";
            rejects([&] { m.scheduled_start(*f.service, "ibkr_tws", parsed(std::move(bad)), true, "", start); });
            f.peer->state_.managed_accounts({true,{"DU1234567", "DU7654321"}}); f.service->poll();
            rejects([&] { m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), true, "", start); });
            f.peer->state_.managed_accounts({true,{"DU7654321"}}); f.service->poll();
            rejects([&] { m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), true, "", start); });
            f.peer->state_.managed_accounts({true,{"DU1234567"}}); f.service->poll();
            wall = collection_open - 1; rejects([&] { m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), true, "", start); });
            wall = collection_open + 27000000; rejects([&] { m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), true, "", start); });
            check(f.peer->subscriptions == 0, "Invalid scope cannot subscribe");
            wall = collection_open; auto result = parsed(m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), true, "", start));
            check(result["requested_plan"]["segments_requested"].i() == 30, "Full session requests 30 fifteen-minute segments");
            check(result["requested_plan"]["requested_seconds"].i() == 27000 && result["requested_plan"]["rows"].i() == 50, "Scheduled settings fixed independently of manual bounds");
            m.stop(*f.service);
            const auto journal = orderbook_jobs::read_file(f.root / "depth-collections" / m.current_id() / "state.json", 131072);
            check(journal.find("DU1234567") == std::string::npos && journal.find("expected_account") == std::string::npos, "Account binding never saved or published");
            Fixture wrong("ibkr_tws", true, "QQQ"); Manager guarded(wrong.store, [&] { return start; }, [&] { return collection_open; });
            rejects([&] { guarded.scheduled_start(*wrong.service, "ibkr_tws", scheduled_request(), true, "", start); });
            check(wrong.peer->subscriptions == 0, "ConId alone cannot impersonate the fixed SPY stock identity");
        });
        test("scheduled identity is idempotent even after stop and restart", [] {
            Fixture f("ibkr_tws", true); auto wall = collection_open; auto steady = Clock::now();
            Manager m(f.store, [&] { return steady; }, [&] { return wall; });
            auto first = parsed(m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), true, "", steady));
            auto repeated = parsed(m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), false, "clock_checking", steady));
            check(field(first["requested_plan"], "collection_id") == field(repeated["requested_plan"], "collection_id") && f.peer->subscriptions == 1, "Uncertain start retries reconcile exact original intent");
            auto conflicting = Json(scheduled_request()); conflicting["close_ms"] = collection_open + 1800000;
            rejects([&] { m.scheduled_start(*f.service, "ibkr_tws", parsed(std::move(conflicting)), true, "", steady); });
            f.delivered(); m.stop(*f.service);
            wall = collection_open + 86400000;
            auto done = parsed(m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), true, "", steady));
            check(field(done["requested_plan"], "state") == "stopped" && f.peer->subscriptions == 1, "Finished same-day key never starts again even outside window");
            Manager reopened(f.store, [&] { return steady; }, [&] { return wall; });
            done = parsed(reopened.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), false, "broker_not_ready", steady));
            check(!reopened.active() && f.peer->subscriptions == 1 && field(done["requested_plan"], "state") == "stopped", "Restart retains idempotency without account or broker mutation");
        });
        test("market close cuts a late final segment with committed stop evidence", [] {
            Fixture f("ibkr_tws", true); auto wall = collection_open + 27000000 - 30000; const auto start = Clock::now(); auto steady = start;
            Manager m(f.store, [&] { return steady; }, [&] { return wall; });
            m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), true, "", steady); f.delivered();
            check(parsed(m.status())["current"]["segments_requested"].i() == 1, "Late start records only remaining partial segment");
            steady += std::chrono::seconds(29); wall += 29000; m.tick(*f.service, "ibkr_tws", true, "", steady);
            check(m.active(), "Capture remains active strictly before close");
            steady += std::chrono::seconds(1); wall += 1000; m.tick(*f.service, "ibkr_tws", true, "", steady);
            const auto result = parsed(m.status());
            check(!m.active() && field(result["current"], "reason") == "collection_window_closed" && field(result["current"], "state") == "stopped", "Market close is explicit, not a complete 15-minute timer claim");
            check(result["current"]["segments"][0]["finalized"].b() && field(result["current"]["segments"][0], "raw_committed_sequence") == field(result["current"]["segments"][0], "committed_sequence"), "Short final segment has actual durable tail");
            check(f.peer->subscriptions == 1 && f.peer->cancellations == 1, "Close cannot replace the request");
        });
        test("wall clock regression cannot extend native market close deadline", [] {
            Fixture f("ibkr_tws", true); auto wall = collection_open; const auto start = Clock::now(); auto steady = start;
            Manager m(f.store, [&] { return steady; }, [&] { return wall; });
            m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(collection_open, collection_open + 60000), true, "", steady); f.delivered();
            wall -= 3600000; steady += std::chrono::seconds(60); m.tick(*f.service, "ibkr_tws", true, "", steady);
            check(!m.active() && field(parsed(m.status())["current"], "reason") == "collection_window_closed" && f.peer->subscriptions == 1, "Anchored steady deadline survives backwards UTC clock");
        });
        test("all thirty full-session segments close before market boundary", [] {
            Fixture f("ibkr_tws", true); auto wall = collection_open; auto steady = Clock::now();
            f.peer->request_clock = [&] { return steady; };
            Manager m(f.store, [&] { return steady; }, [&] { return wall; });
            m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), true, "", steady);
            for (int segment = 1; segment <= 30; ++segment) {
                f.delivered(); steady += std::chrono::seconds(900); wall += 900000;
                m.tick(*f.service, "ibkr_tws", true, "", steady);
                check(m.active() == (segment < 30), "Only the regular-hours window may rotate");
            }
            const auto result = parsed(m.status());
            check(f.peer->subscriptions == 30 && f.peer->cancellations == 30 && result["current"]["segments_completed"].i() == 30,
                "All native subscriptions have durable paired stops");
            check(field(result["current"], "reason") == "collection_window_closed" && result["current"]["elapsed_seconds"].d() == 27000,
                "Full schedule closes at exact session endpoint");
            Manager reopened(f.store); check(parsed(reopened.status())["current"]["segments"].size() == 30, "Extended scheduled journal reloads intact");
        });
        test("slow segment drain crossing close cannot open another subscription", [] {
            Fixture f("ibkr_tws", true); auto wall = collection_open; const auto start = Clock::now(); auto steady = start;
            Manager m(f.store, [&] { return steady; }, [&] { return wall; });
            m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(collection_open, collection_open + 905000), true, "", steady); f.delivered();
            f.peer->before = [&](const char* action) { if (std::string(action) == "cancel") { steady = start + std::chrono::seconds(910); wall = collection_open + 910000; } };
            m.tick(*f.service, "ibkr_tws", true, "", start + std::chrono::seconds(900));
            check(!m.active() && field(parsed(m.status())["current"], "reason") == "collection_window_closed", "Fresh clock checked after durable close");
            check(f.peer->subscriptions == 1 && f.peer->cancellations == 1, "Close during drain has no post-close subscription");
        });
        test("paper binding changes interrupt without reconnect or replacement", [] {
            Fixture f("ibkr_tws", true); auto wall = collection_open; const auto start = Clock::now();
            Manager m(f.store, [&] { return start; }, [&] { return wall; });
            m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), true, "", start); f.delivered();
            f.peer->state_.managed_accounts({true,{"DU7654321"}}); f.service->poll();
            m.tick(*f.service, "ibkr_tws", true, "", start + std::chrono::seconds(1));
            check(!m.active() && field(parsed(m.status())["current"], "reason") == "paper_account_binding_changed", "Fresh account binding is continuously enforced");
            check(f.peer->subscriptions == 1, "No replacement after account change");
        });
        test("scheduled restart remains interrupted and manual ownership cannot be adopted", [] {
            Fixture f("ibkr_tws", true); auto wall = collection_open; const auto start = Clock::now();
            Manager m(f.store, [&] { return start; }, [&] { return wall; });
            m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), true, "", start); f.delivered();
            Manager reopened(f.store, [&] { return start; }, [&] { return wall; });
            const auto recovered = parsed(reopened.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), true, "", start));
            check(field(recovered["requested_plan"], "reason") == "server_restart" && !reopened.active() && f.peer->subscriptions == 1, "Recovery does not resume authorized prior intent");
            m.stop(*f.service);
            auto manual = Json(request()); manual["contract_id"] = "756733";
            m.start(*f.service, "ibkr_tws", parsed(std::move(manual)), true, "", start);
            rejects([&] { m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(collection_open, collection_open + 27000000, "22222222222222222222222222222222"), true, "", start); });
            check(f.peer->subscriptions == 2 && m.active(), "Manual request retained unchanged"); m.stop(*f.service);
        });
        test("catalog archives oldest terminal journals without deleting bytes", [] {
            Fixture f; Manager m(f.store);
            std::string oldest, original;
            for (int i = 0; i < 101; ++i) {
                if (i) {
                    // Separate real collection days would not hit ten requests
                    // per second. Reset the synthetic peer's pacing generation.
                    f.service->disconnect(); f.service->connect(); f.service->poll();
                    ContractQuery q; q.symbol = "SYNTHETIC"; f.service->resolve(q); f.service->poll();
                }
                m.start(*f.service, "mock", request(60, 1), true); m.stop(*f.service);
                if (i == 0) { oldest = m.current_id(); original = orderbook_jobs::read_file(f.root / "depth-collections" / oldest / "state.json", 131072); }
            }
            check(fs::exists(f.root / "depth-collection-history" / oldest / "state.json"), "Oldest terminal plan moved into private history");
            check(orderbook_jobs::read_file(f.root / "depth-collection-history" / oldest / "state.json", 131072) == original, "Original journal bytes retained");
            check(std::distance(fs::directory_iterator(f.root / "depth-collections"), fs::directory_iterator{}) == 100, "Active catalog remains bounded");
            check(parsed(m.status())["plans"].size() == 20, "Public catalog remains newest twenty");
            Manager reopened(f.store); check(!reopened.active() && parsed(reopened.status())["invalid_records"].i() == 0, "History does not overflow restart catalog");
        });
        test("archived scheduled idempotency survives newer manual plans", [] {
            Fixture f("ibkr_tws", true); auto wall = collection_open; auto steady = Clock::now();
            Manager m(f.store, [&] { return steady; }, [&] { return wall; });
            m.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), true, "", steady); m.stop(*f.service);
            auto manual = Json(request(60, 1)); manual["contract_id"] = "756733"; const auto plain = parsed(std::move(manual));
            for (int i = 0; i < 100; ++i) {
                f.service->disconnect(); f.service->connect(); f.service->poll();
                ContractQuery q; q.symbol = "SPY"; f.service->resolve(q); f.service->poll();
                m.start(*f.service, "ibkr_tws", plain, true); m.stop(*f.service);
            }
            check(fs::exists(f.root / "depth-collection-history" / "11111111111111111111111111111111" / "state.json"), "Scheduled original remains available in history");
            const auto before = f.peer->subscriptions;
            Manager reopened(f.store, [&] { return steady; }, [&] { return wall; });
            const auto result = parsed(reopened.scheduled_start(*f.service, "ibkr_tws", scheduled_request(), false, "clock_checking", steady));
            check(f.peer->subscriptions == before && !reopened.active() && field(result["requested_plan"], "schedule_id") == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "An archived idempotent intent is returned without adopting or replacing current plan");
            check(result["current"]["schedule_id"].t() == crow::json::type::Null, "Newest manual plan identity remains current");
        });
        std::cout << passed << " collection lifecycle tests passed\n"; return 0;
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
