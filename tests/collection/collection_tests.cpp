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
    int subscriptions = 0, cancellations = 0, disconnections = 0;
    bool unknown_cancel = false, unknown_subscribe = false;
    RequestId last = 0;
    std::vector<DepthSpec> requested;
    std::function<void(const char*)> before;
    void connect() override { state_.start(Clock::now()); state_.ready(); }
    void disconnect() noexcept override { ++disconnections; state_.disconnect(); }
    ConnectionState state() const noexcept override { return state_.state(); }
    RequestId resolve(const ContractQuery& q) override {
        auto id = state_.resolve(q, Clock::now()); state_.contract(id, instrument()); state_.contract_end(id); return id;
    }
    RequestId subscribe(const Contract&) override { return 0; }
    bool unsubscribe(RequestId) override { return false; }
    void request_positions(RequestId) override {}
    RequestId subscribe_depth(const DepthSpec& spec) override {
        if (before) before("subscribe");
        ++subscriptions; requested.push_back(spec); last = state_.depth(spec, Clock::now());
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
    storage::TimeSeriesStore store{storage::Config{root, root / "backups"}, "mock"};
    Peer* peer = nullptr;
    std::unique_ptr<ReadOnlyService> service;
    bool expected_store_failure = false;
    Fixture() {
        auto inner = std::make_unique<Peer>(); peer = inner.get();
        service = std::make_unique<ReadOnlyService>(std::make_unique<storage::RecordingBroker>(std::move(inner), store, "mock"));
        service->connect(); service->poll(); ContractQuery q; q.symbol = "SYNTHETIC"; service->resolve(q); service->poll();
    }
    ~Fixture() {
        service->disconnect();
        if (store.status().failed && !expected_store_failure) ++unexpected_store_failures;
        try { store.close(); } catch (const std::exception&) { if (!expected_store_failure) ++unexpected_store_failures; }
        fs::remove_all(root);
    }
    void delivered() { peer->update(0, 0, 0, 101); peer->update(1, 0, 0, 99); service->poll(); }
};
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
        std::cout << passed << " collection lifecycle tests passed\n"; return 0;
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
