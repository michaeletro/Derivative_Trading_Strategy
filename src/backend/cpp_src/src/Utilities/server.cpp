#define CROW_USE_BOOST 1
#include <boost/asio.hpp>
namespace asio = boost::asio;
#include <crow_all.h>
#include <dashboard_routes.hpp>
#include "browser_auth.hpp"
#include <sqlite3.h>
#include "pricing_json.hpp"
#include "greeks_json.hpp"
#include "storage_json.hpp"
#include "history_json.hpp"
#include "research_json.hpp"
#include "experiments_json.hpp"
#include "depth_json.hpp"
#include <dts/recording_broker.hpp>
#include <dts/read_only_service.hpp>
#include <dts/mock_broker.hpp>
#ifdef DTS_WITH_IBKR
#include <dts/tws_broker.hpp>
#endif
#include <atomic>
#include <csignal>
#include <condition_variable>
#include <cstdlib>
#include <iostream>
#include <mutex>
#include <thread>

namespace {
using Json = crow::json::wvalue;
std::string env(const char* key, const std::string& fallback = "") {
    const char* value = std::getenv(key); return value ? value : fallback;
}
int integer(const std::string& value, int minimum, int maximum) {
    std::size_t used = 0; const long number = std::stol(value, &used);
    if (used != value.size() || number < minimum || number > maximum)
        throw std::invalid_argument("Integer outside permitted range");
    return static_cast<int>(number);
}
const char* state_name(dts::ConnectionState state) {
    switch (state) {
        case dts::ConnectionState::Ready: return "ready";
        case dts::ConnectionState::Connecting: return "connecting";
        case dts::ConnectionState::Failed: return "failed";
        default: return "disconnected";
    }
}
const char* snapshot_name(dts::SnapshotStatus state) {
    switch (state) {
        case dts::SnapshotStatus::Pending: return "pending";
        case dts::SnapshotStatus::Complete: return "complete";
        case dts::SnapshotStatus::Failed: return "failed";
        default: return "unavailable";
    }
}
const char* feed_name(dts::MarketDataType type) {
    switch (type) {
        case dts::MarketDataType::Realtime: return "realtime";
        case dts::MarketDataType::Frozen: return "frozen";
        case dts::MarketDataType::Delayed: return "delayed";
        case dts::MarketDataType::DelayedFrozen: return "delayed_frozen";
        default: return "simulation";
    }
}
Json contract_json(const dts::Contract& c) {
    Json j; j["contract_id"] = c.id; j["symbol"] = c.symbol;
    j["exchange"] = c.exchange; j["currency"] = c.currency; j["multiplier"] = c.multiplier;
    j["security_type"] = c.security_type == dts::SecurityType::Option ? "OPT" : "STK";
    if (c.option) {
        j["expiry"] = c.option->expiry; j["strike"] = c.option->strike;
        j["right"] = c.option->right == dts::OptionRight::Call ? "C" : "P";
        j["exercise_style"] = "unknown";
    }
    return j;
}
Json quote_json(const dts::Quote& quote, dts::Clock::time_point now) {
    Json j; j["contract_id"] = quote.contract_id; j["data_type"] = feed_name(quote.data_type);
    const auto mid = quote.mid(now, std::chrono::seconds(5));
    j["mid"] = mid ? Json(*mid) : Json(nullptr); j["indicative_only"] = true;
    const auto side = [now](const std::optional<dts::QuoteSide>& value) {
        if (!value) return Json(nullptr);
        Json out; out["price"] = value->price;
        out["receipt_age_ms"] = std::chrono::duration_cast<std::chrono::milliseconds>(now - value->received_at).count();
        return out;
    };
    j["bid"] = side(quote.bid); j["ask"] = side(quote.ask); return j;
}
Json positions_json(const dts::PositionsView& view, bool simulation) {
    Json j; j["status"] = snapshot_name(view.status); j["simulation"] = simulation;
    j["snapshot_not_stream"] = true; std::vector<Json> rows;
    for (const auto& position : view.positions) {
        Json row; row["account"] = position.account; row["quantity"] = position.quantity;
        row["contract"] = contract_json(position.contract); rows.push_back(std::move(row));
    }
    j["positions"] = view.status == dts::SnapshotStatus::Complete ? Json(std::move(rows)) : Json(nullptr);
    if (view.completed_at) j["completed_at_unix_ms"] = std::chrono::duration_cast<std::chrono::milliseconds>(view.completed_at->time_since_epoch()).count();
    return j;
}
std::string text(const crow::json::rvalue& j, const char* key, const std::string& fallback = "") {
    if (!j.has(key)) return fallback;
    if (j[key].t() != crow::json::type::String) throw std::invalid_argument("Expected string");
    const std::string s = j[key].s();
    if (s.size() > 64 || s.find_first_of("\r\n\t") != std::string::npos || s.find('\0') != std::string::npos)
        throw std::invalid_argument("Invalid text value");
    return s;
}
double number(const crow::json::rvalue& j, const char* key) {
    if (!j.has(key) || j[key].t() != crow::json::type::Number) throw std::invalid_argument("Expected number");
    const double value = j[key].d();
    if (!std::isfinite(value)) throw std::invalid_argument("Nonfinite number");
    return value;
}
crow::json::rvalue object(const crow::request& req) {
    auto j = crow::json::load(req.body);
    if (!j || j.t() != crow::json::type::Object) throw std::invalid_argument("Expected JSON object");
    return j;
}

// Existing asset_data source stays read-only. Returned bars are archived in a
// separate database before the successful response is sent to the browser.
class AssetRepository {
    sqlite3* db_ = nullptr;
    std::string dataset_;
    dts::storage::TimeSeriesStore& store_;
public:
    explicit AssetRepository(const std::string& path, dts::storage::TimeSeriesStore& store)
        : dataset_(std::filesystem::weakly_canonical(path).string()), store_(store) {
        if (sqlite3_open_v2(path.c_str(), &db_, SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX, nullptr) != SQLITE_OK) {
            if (db_) sqlite3_close(db_);
            db_ = nullptr;
        }
        if (db_) sqlite3_busy_timeout(db_, 1000);
    }
    ~AssetRepository() { if (db_) sqlite3_close(db_); }
    AssetRepository(const AssetRepository&) = delete;
    AssetRepository& operator=(const AssetRepository&) = delete;
    bool available() const noexcept { return db_ != nullptr; }
    Json query(const crow::request& req) {
        if (!db_) throw std::logic_error("Asset database unavailable");
        store_.require_healthy();
        const auto parameter = [&req](const char* name) {
            const auto* p = req.url_params.get(name); return p ? std::string(p) : std::string();
        };
        const auto ticker = parameter("ticker"), start = parameter("start"), end = parameter("end");
        if (ticker.size() > 64 || start.size() > 32 || end.size() > 32) throw std::invalid_argument("Filter too long");
        const auto limit_text = parameter("limit");
        const int limit = limit_text.empty() ? 100 : integer(limit_text, 1, 5000);
        const char* sql = "SELECT id,ticker,open_price,close_price,high_price,low_price,volume,date FROM asset_data "
            "WHERE (?1='' OR ticker=?1) AND (?2='' OR date>=?2) AND (?3='' OR date<=?3) ORDER BY date ASC LIMIT ?4";
        sqlite3_stmt* raw = nullptr;
        if (sqlite3_prepare_v2(db_, sql, -1, &raw, nullptr) != SQLITE_OK) throw std::runtime_error("Asset query unavailable");
        std::unique_ptr<sqlite3_stmt, decltype(&sqlite3_finalize)> statement(raw, sqlite3_finalize);
        sqlite3_bind_text(raw, 1, ticker.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(raw, 2, start.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_text(raw, 3, end.c_str(), -1, SQLITE_TRANSIENT);
        sqlite3_bind_int(raw, 4, limit);
        std::vector<Json> rows;
        std::vector<dts::storage::Bar> archived;
        int status = SQLITE_OK;
        while ((status = sqlite3_step(raw)) == SQLITE_ROW) {
            Json row; row["id"] = static_cast<std::int64_t>(sqlite3_column_int64(raw, 0));
            for (const auto column : {1, 7}) {
                const auto* value = sqlite3_column_text(raw, column);
                row[column == 1 ? "ticker" : "date"] = value ? reinterpret_cast<const char*>(value) : "";
            }
            dts::storage::Bar bar;
            bar.source_row_id = std::to_string(sqlite3_column_int64(raw, 0));
            const auto read_text = [raw](int col) {
                const auto* value = sqlite3_column_text(raw, col);
                return value ? std::string(reinterpret_cast<const char*>(value), sqlite3_column_bytes(raw,col)) : std::string();
            };
            bar.symbol=read_text(1); bar.time_text=read_text(7);
            const char* prices[] = {"open_price", "close_price", "high_price", "low_price"};
            std::optional<double>* values[] = {&bar.open,&bar.close,&bar.high,&bar.low};
            for (int i = 0; i < 4; ++i) {
                if (sqlite3_column_type(raw,i+2)!=SQLITE_NULL) {
                    if (sqlite3_column_type(raw,i+2)!=SQLITE_FLOAT && sqlite3_column_type(raw,i+2)!=SQLITE_INTEGER)
                        throw std::runtime_error("Asset price has an invalid source type");
                    *values[i]=sqlite3_column_double(raw,i+2);
                }
                row[prices[i]] = *values[i] ? Json(**values[i]) : Json(nullptr);
            }
            if(sqlite3_column_type(raw,6)!=SQLITE_NULL) {
                if(sqlite3_column_type(raw,6)!=SQLITE_INTEGER)throw std::runtime_error("Asset volume is not an integer");
                bar.volume=static_cast<std::int64_t>(sqlite3_column_int64(raw,6));
            }
            row["volume"]=bar.volume ? Json(*bar.volume) : Json(nullptr);
            archived.push_back(std::move(bar));
            rows.push_back(std::move(row));
        }
        if (status != SQLITE_DONE) throw std::runtime_error("Asset query did not complete");
        const auto saved=store_.record_asset_read({dataset_,ticker,start,end,limit},archived);
        Json result; result["count"] = rows.size(); result["results"] = std::move(rows);
        result["recording"]["durable"] = true; result["recording"]["read_id"] = std::to_string(saved.id);
        result["recording"]["new_observations"] = saved.inserted; return result;
    }
};

class Application {
public:
    Application() : mode_(env("DTS_BROKER", "none")), token_(env("DTS_API_TOKEN")),
        port_(integer(env("HTTP_PORT", "8080"), 1024, 65535)),
        store_(dts::storage::Config::from_environment(), mode_),
        assets_(env("DB_PATH", "quant_data.db"), store_), broker_(make_broker()), history_(store_) {
        if (env("ENABLE_IB_WS", "false") != "false" && env("ENABLE_IB_WS") != "0")
            throw std::invalid_argument("Client Portal relay is retired; configure DTS_BROKER instead");
        if (env("DB_FAIL_FAST", "false") == "true" && !assets_.available())
            throw std::runtime_error("Configured asset database unavailable");
        // Start launch-code expiry after database validation/migration finishes.
        const auto launch = env("DTS_BROWSER_BOOTSTRAP_CODE");
        if (!launch.empty()) {
            if (token_.size() < 24) throw std::invalid_argument("Local auto sign-in requires a configured credential");
            browser_sessions_.seed(launch, "127.0.0.1:" + std::to_string(port_));
        }
        ::unsetenv("DTS_BROWSER_BOOTSTRAP_CODE");
        ::unsetenv("DTS_LAUNCH_NONCE");
        routes();
        const auto storage=store_.status();
        std::cout << "Time-series database opened: " << storage.database << '\n'
                  << "Automatic shutdown backups: " << storage.backup_directory << std::endl;
        if(storage.interrupted_runs)std::cerr << "Previous unclean run(s) detected: committed history recovered; gaps may exist.\n";
    }
    ~Application() { stop(); }
    void run() {
        worker_ = std::thread([this] {
            std::unique_lock<std::mutex> lock(broker_mutex_);
            while (!stopping_) {
                try { broker_.poll(); history_.tick(broker_); }
                catch (...) { broker_.disconnect(); worker_failed_ = true; }
                wake_.wait_for(lock, std::chrono::milliseconds(10), [this] { return stopping_; });
            }
            // Stop has a finite acquisition cutoff. Persist all delivered events
            // before disconnecting and closing the store. Never wait until exit
            // to save observations that were received earlier in the session.
            try { broker_.poll(); } catch (...) { worker_failed_ = true; }
            broker_.disconnect();
        });
        try {
        app_.signal_add(SIGHUP).bindaddr("127.0.0.1").port(static_cast<std::uint16_t>(port_)).concurrency(2).run();
        } catch (...) {
            stop(); try { store_.close(false); } catch (...) {}
            throw;
        }
        stop();
        std::cout << "Shutdown: acquisition stopped. Finalizing recording and creating SQLite backup..." << std::endl;
        store_.close(!worker_failed_);
        std::cout << "Shutdown complete: " << store_.status().last_backup << std::endl;
    }
private:
    crow::SimpleApp app_;
    std::string mode_, token_;
    int port_;
    dts::local_auth::Sessions browser_sessions_;
    const std::string launch_nonce_ = env("DTS_LAUNCH_NONCE");
    dts::storage::TimeSeriesStore store_;
    AssetRepository assets_;
    dts::ReadOnlyService broker_;
    dts::storage::HistoricalManager history_;
    // Non-secret instance identifier prevents charts joining observations across restarts.
    const std::string instance_ = std::to_string(std::chrono::system_clock::now().time_since_epoch().count());
    std::mutex broker_mutex_, database_mutex_, pricing_mutex_;
    std::condition_variable wake_;
    std::thread worker_;
    bool stopping_ = false, worker_failed_ = false;
    std::unique_ptr<dts::IBroker> make_broker() {
        if (mode_ == "none") return {};
        if (mode_ == "mock") return std::make_unique<dts::storage::RecordingBroker>(std::make_unique<dts::MockBroker>(), store_, "mock");
        if (mode_ != "tws") throw std::invalid_argument("DTS_BROKER must be none, mock, or tws");
        if (token_.size() < 24) throw std::invalid_argument("TWS mode requires DTS_API_TOKEN of at least 24 characters");
#ifdef DTS_WITH_IBKR
        dts::TwsConfig config;
        config.host = env("IB_HOST", "127.0.0.1");
        config.port = integer(env("IB_PORT", "4002"), 1, 65535);
        config.client_id = integer(env("IB_CLIENT_ID", "17"), 1, 2147483647);
        config.market_data_type = integer(env("IB_MARKET_DATA_TYPE", "3"), 1, 4);
        config.timeout = std::chrono::milliseconds(integer(env("IB_TIMEOUT_MS", "10000"), 100, 60000));
        return std::make_unique<dts::storage::RecordingBroker>(std::make_unique<dts::TwsBroker>(config), store_, "ibkr_tws");
#else
        throw std::invalid_argument("Rebuild with DTS_WITH_IBKR=ON to select TWS");
#endif
    }
    void stop() noexcept {
        { std::lock_guard<std::mutex> lock(broker_mutex_); stopping_ = true; }
        wake_.notify_all();
        if (worker_.joinable()) worker_.join();
    }
    crow::response response(Json body, int status = 200) const {
        crow::response result(std::move(body)); result.code = status;
        result.set_header("Cache-Control", "no-store");
        result.set_header("Referrer-Policy", "no-referrer");
        result.set_header("X-Content-Type-Options", "nosniff"); return result;
    }
    crow::response message(int status, const char* text_value) const {
        Json j; j["error"] = text_value; return response(std::move(j), status);
    }
    std::optional<crow::response> local_guard(const crow::request& req) const {
        const auto host = req.get_header_value("Host");
        if (host != "127.0.0.1:" + std::to_string(port_) && host != "localhost:" + std::to_string(port_))
            return message(403, "Invalid Host header");
        const auto origin = req.get_header_value("Origin");
        if (!origin.empty() && origin != "http://" + host) return message(403, "Cross-origin access denied");
        const auto site = req.get_header_value("Sec-Fetch-Site");
        if (!site.empty() && site != "same-origin") return message(403, "Cross-origin API access denied");
        if (req.body.size() > 8192) return message(413, "Request too large");
        return {};
    }
    bool browser_request(const crow::request& req, bool mutation = false) const {
        // SameSite ignores ports. Require exact Origin for mutations and either
        // exact Origin or same-origin Fetch Metadata for GET cookie auth.
        const auto origin = req.get_header_value("Origin");
        const bool exact = origin == "http://" + req.get_header_value("Host");
        return req.get_header_value("X-DTS-Local-Request") == "1" &&
            (mutation ? exact : (exact || req.get_header_value("Sec-Fetch-Site") == "same-origin"));
    }
    std::string browser_cookie(const crow::request& req) const {
        return dts::local_auth::cookie_value(req.get_header_value("Cookie"), port_);
    }
    bool bearer(const crow::request& req) const {
        return !token_.empty() && dts::local_auth::equal_secret(req.get_header_value("Authorization"), "Bearer " + token_);
    }
    bool browser_session(const crow::request& req) {
        return browser_request(req, req.method != crow::HTTPMethod::GET) &&
            browser_sessions_.valid(browser_cookie(req), req.get_header_value("Host"));
    }
    template<class Function> crow::response guarded(const crow::request& req, Function fn) {
        if (auto error = local_guard(req)) return std::move(*error);
        // An explicitly wrong bearer must not fall back to a valid cookie.
        if (!token_.empty() && !(req.get_header_value("Authorization").empty() ? browser_session(req) : bearer(req)))
            return message(401, "Local sign-in or Bearer token required");
        try { return response(fn()); }
        catch (const std::out_of_range&) { return message(404, "Unknown resource or numeric value outside range"); }
        catch (const std::invalid_argument& e) { return message(400, e.what()); }
        catch (const std::length_error& e) { return message(429, e.what()); }
        catch (const std::logic_error& e) { return message(409, e.what()); }
        catch (...) { return message(503, "Service operation unavailable"); }
    }
    void auth_routes() {
        CROW_ROUTE(app_, "/api/auth/status")([this](const crow::request& req) {
            if (auto error = local_guard(req)) return std::move(*error);
            Json j; j["schema_version"] = 1; j["local_signin"] = token_.size() >= 24;
            j["authenticated"] = !token_.empty() && browser_session(req);
            // Public startup correlation value, NOT an authenticator.
            j["launch_nonce"] = launch_nonce_; return response(std::move(j));
        });
        CROW_ROUTE(app_, "/api/auth/launch").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            if (auto error = local_guard(req)) return std::move(*error);
            if (token_.size() < 24 || !bearer(req)) return message(401, "Saved local profile credential required");
            try {
                dts::pricing::http::keys(object(req), {});
                Json j; j["schema_version"] = 1; j["code"] = browser_sessions_.issue(req.get_header_value("Host"));
                j["expires_in_seconds"] = 60; return response(std::move(j));
            } catch (const std::length_error&) { return message(429, "Too many pending local sign-ins"); }
              catch (const std::invalid_argument&) { return message(400, "Expected empty JSON object"); }
              catch (...) { return message(503, "Local sign-in unavailable"); }
        });
        CROW_ROUTE(app_, "/api/auth/exchange").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            if (auto error = local_guard(req)) return std::move(*error);
            if (!browser_request(req, true)) return message(403, "Same-origin browser request required");
            try {
                const auto body = object(req); dts::pricing::http::keys(body, {"code"});
                const auto code = text(body, "code");
                const auto session = browser_sessions_.exchange(code, req.get_header_value("Host"), browser_cookie(req));
                if (session.empty()) return message(401, "Local sign-in link expired or already used; reopen from the launcher");
                Json j; j["schema_version"] = 1; j["authenticated"] = true; j["expires_in_seconds"] = 43200;
                auto out = response(std::move(j)); out.set_header("Set-Cookie", dts::local_auth::set_cookie(session, port_)); return out;
            } catch (const std::length_error&) { return message(429, "Too many local browser sessions"); }
              catch (const std::invalid_argument&) { return message(400, "Invalid local sign-in request"); }
              catch (...) { return message(503, "Local sign-in unavailable"); }
        });
        CROW_ROUTE(app_, "/api/auth/logout").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            if (auto error = local_guard(req)) return std::move(*error);
            if (!browser_request(req, true)) return message(403, "Same-origin browser request required");
            browser_sessions_.revoke(browser_cookie(req), req.get_header_value("Host"));
            Json j; j["signed_out"] = true;
            auto out = response(std::move(j)); out.set_header("Set-Cookie", dts::local_auth::clear_cookie(port_)); return out;
        });
    }
    Json status() const {
        Json j; j["mode"] = mode_; j["enabled"] = broker_.enabled();
        j["state"] = state_name(broker_.state()); j["connected"] = broker_.state() == dts::ConnectionState::Ready;
        j["read_only"] = true; j["simulation"] = mode_ == "mock"; j["worker_failed"] = worker_failed_;
        std::vector<Json> errors;
        for (const auto& e : broker_.errors()) { Json item; item["request_id"] = e.request_id; item["code"] = e.code; errors.push_back(std::move(item)); }
        j["errors"] = std::move(errors); return j;
    }
    void routes() {
        auth_routes();
        CROW_ROUTE(app_, "/api/hedging/run").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{const auto request=dts::hedging_http::parse(object(req));
                std::unique_lock<std::mutex> lock(pricing_mutex_,std::try_to_lock);
                if(!lock.owns_lock())throw std::length_error("Another research calculation is running");
                return dts::hedging_http::run(request);});
        });
        CROW_ROUTE(app_, "/api/sde/run").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{const auto request=dts::sde_http::parse(object(req));
                std::unique_lock<std::mutex> lock(pricing_mutex_,std::try_to_lock);
                if(!lock.owns_lock())throw std::length_error("Research calculation busy; retry explicitly later");
                return dts::sde_http::run(request);});
        });
        CROW_ROUTE(app_, "/api/experiments/list").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{return dts::experiments_http::catalog(store_,object(req));});
        });
        CROW_ROUTE(app_, "/api/experiments/view").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{return dts::experiments_http::view(store_,object(req));});
        });
        CROW_ROUTE(app_, "/api/experiments/compute").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{const auto j=object(req);dts::pricing::http::keys(j,{"kind","name","request"});
                const auto kind=dts::pricing::http::text(j,"kind"),label=dts::research_http::name(j);
                std::unique_lock<std::mutex> lock(pricing_mutex_,std::try_to_lock);
                if(!lock.owns_lock())throw std::length_error("Research calculation busy; reconcile catalog before repeating a save");
                return dts::experiments_http::compute(store_,kind,label,j["request"]);});
        });
        CROW_ROUTE(app_, "/api/experiments/rerun").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{std::unique_lock<std::mutex> lock(pricing_mutex_,std::try_to_lock);
                if(!lock.owns_lock())throw std::length_error("Research calculation busy; reconcile catalog before repeating a save");
                return dts::experiments_http::rerun(store_,object(req));});
        });
        CROW_ROUTE(app_, "/api/research/snapshots/create").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{const auto j=object(req);dts::research_http::fields(j,{"dataset_id","start_s","end_s","name"});
                std::unique_lock<std::mutex> lock(pricing_mutex_,std::try_to_lock);
                if(!lock.owns_lock())throw std::length_error("Research operation busy; retry explicitly later");
                return dts::research_http::snapshot_json(store_.create_snapshot(dts::research_http::id(j,"dataset_id"),dts::history_http::window(j),dts::research_http::name(j)));});
        });
        CROW_ROUTE(app_, "/api/research/snapshots/list").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{return dts::research_http::catalog(store_,object(req),true);});
        });
        CROW_ROUTE(app_, "/api/research/snapshots/view").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{const auto j=object(req);dts::research_http::fields(j,{"snapshot_id"});return dts::research_http::snapshot_json(store_.snapshot(dts::research_http::id(j,"snapshot_id")));});
        });
        CROW_ROUTE(app_, "/api/research/snapshots/export").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{const auto j=object(req);dts::research_http::fields(j,{"snapshot_id"});return dts::research_http::snapshot_json(store_.snapshot(dts::research_http::id(j,"snapshot_id")),true);});
        });
        CROW_ROUTE(app_, "/api/research/replay").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{const auto j=object(req);dts::research_http::fields(j,{"snapshot_id","config","through_ordinal"});
                const auto c=dts::research_http::request_config(j);const auto count=dts::research_http::bounded_integer(j,"through_ordinal",0,2000);
                std::unique_lock<std::mutex> lock(pricing_mutex_,std::try_to_lock);
                if(!lock.owns_lock())throw std::length_error("Research operation busy; retry explicitly later");
                return dts::research_http::run(store_.snapshot(dts::research_http::id(j,"snapshot_id")),c,static_cast<std::size_t>(count));});
        });
        CROW_ROUTE(app_, "/api/research/experiments/create").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{const auto j=object(req);dts::research_http::fields(j,{"snapshot_id","name","config"});
                const auto c=dts::research_http::request_config(j);const auto label=dts::research_http::name(j);
                std::unique_lock<std::mutex> lock(pricing_mutex_,std::try_to_lock);
                if(!lock.owns_lock())throw std::length_error("Research operation busy; reconcile before repeating a save");
                return dts::research_http::save(store_,dts::research_http::id(j,"snapshot_id"),label,c);});
        });
        CROW_ROUTE(app_, "/api/research/experiments/list").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{return dts::research_http::catalog(store_,object(req),false);});
        });
        CROW_ROUTE(app_, "/api/research/experiments/view").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{const auto j=object(req);dts::research_http::fields(j,{"experiment_id"});return dts::research_http::experiment_json(store_.experiment(dts::research_http::id(j,"experiment_id")));});
        });
        CROW_ROUTE(app_, "/api/research/experiments/rerun").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{const auto j=object(req);dts::research_http::fields(j,{"experiment_id","name"});const auto label=dts::research_http::name(j);
                std::unique_lock<std::mutex> lock(pricing_mutex_,std::try_to_lock);
                if(!lock.owns_lock())throw std::length_error("Research operation busy; reconcile before repeating a save");
                const auto parent=store_.experiment(dts::research_http::id(j,"experiment_id"));
                if(parent.engine_version!=dts::research::engine_version)throw std::logic_error("Saved engine version is incompatible with this rerun engine");
                return dts::research_http::save(store_,parent.snapshot_id,label,dts::research_http::verified_experiment_config(parent),parent.id);});
        });

        CROW_ROUTE(app_, "/api/depth/subscribe").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{const auto j=object(req);dts::history_http::fields(j,{"contract_id","venue","rows"});
                const auto id=dts::history_http::id(j,"contract_id");
                const auto rows=dts::research_http::bounded_integer(j,"rows",1,10);
                const auto venue=text(j,"venue");std::lock_guard<std::mutex> lock(broker_mutex_);
                Json out=dts::depth_http::conventions();out["request_id"]=std::to_string(broker_.subscribe_depth(id,venue,rows));
                out["source"]=mode_=="mock"?"mock":"ibkr_tws";out["synthetic"]=mode_=="mock";return out;});
        });
        CROW_ROUTE(app_, "/api/depth/unsubscribe").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{const auto j=object(req);dts::history_http::fields(j,{"request_id"});
                const auto id=dts::history_http::id(j,"request_id");std::lock_guard<std::mutex> lock(broker_mutex_);
                Json out;out["schema_version"]=1;out["cancelled"]=broker_.unsubscribe_depth(id);return out;});
        });
        CROW_ROUTE(app_, "/api/depth/current")([this](const crow::request& req) {
            return guarded(req,[&]{std::lock_guard<std::mutex> lock(broker_mutex_);
                auto j=dts::depth_http::current(broker_);j["source"]=mode_=="none"?"disabled":mode_=="mock"?"mock":"ibkr_tws";
                j["synthetic"]=mode_=="mock";return j;});
        });
        CROW_ROUTE(app_, "/api/depth/sessions").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{return dts::depth_http::sessions(store_,object(req));});
        });
        CROW_ROUTE(app_, "/api/depth/events").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{return dts::depth_http::events(store_,object(req));});
        });
        CROW_ROUTE(app_, "/api/history/datasets")([this](const crow::request& req) {
            return guarded(req,[&]{Json j;j["datasets"]=dts::history_http::rows(store_.historical_catalog());j["limit"]=200;j["recorded_not_live"]=true;return j;});
        });
        CROW_ROUTE(app_, "/api/history/view").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{const auto j=object(req);dts::history_http::fields(j,{"dataset_id","start_s","end_s"});
                std::lock_guard<std::mutex> lock(broker_mutex_);
                return dts::history_http::view(store_,dts::history_http::id(j,"dataset_id"),dts::history_http::window(j));});
        });
        CROW_ROUTE(app_, "/api/history/request").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{const auto j=object(req);
                dts::history_http::fields(j,{"dataset_id","contract_id","start_s","end_s","bar_size","price_type","use_rth","policy"});
                if(j.has("dataset_id")==j.has("contract_id"))throw std::invalid_argument("Choose one saved dataset or explicitly resolved contract");
                std::lock_guard<std::mutex> lock(broker_mutex_);dts::HistorySpec spec;
                if(j.has("dataset_id")) {
                    if(j.has("bar_size")||j.has("price_type")||j.has("use_rth"))throw std::invalid_argument("Saved dataset conventions are immutable");
                    spec=store_.historical_spec(dts::history_http::id(j,"dataset_id"));
                }else{
                    spec.contract=broker_.contract(dts::history_http::integer(j,"contract_id"));
                    spec.bar_size=text(j,"bar_size","1 day");spec.price_type=text(j,"price_type","TRADES");
                    if(!j.has("use_rth")||(j["use_rth"].t()!=crow::json::type::True&&j["use_rth"].t()!=crow::json::type::False))throw std::invalid_argument("use_rth must be an explicit Boolean");
                    spec.use_rth=j["use_rth"].b();
                }
                const auto w=dts::history_http::window(j);const auto policy=text(j,"policy","saved");
                const auto plan=history_.request(spec,w,policy,broker_.state());
                Json out=dts::history_http::view(store_,plan.dataset_id,w);std::vector<Json> queued;
                for(auto id:plan.queued_ids)queued.emplace_back(std::to_string(id));
                out["new_request_ids"]=std::move(queued);
                out["dispatch_spacing_seconds"]=15;return out;
            });
        });
        CROW_ROUTE(app_, "/api/history/cancel").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req,[&]{dts::history_http::fields(object(req),{});std::lock_guard<std::mutex> lock(broker_mutex_);history_.cancel(broker_);Json j;j["cancelled_pending_history"]=true;return j;});
        });
        CROW_ROUTE(app_, "/api/storage/status")([this](const crow::request& req) {
            return guarded(req, [&] { return dts::storage::http::status(store_.status()); });
        });
        CROW_ROUTE(app_, "/api/storage/series")([this](const crow::request& req) {
            return guarded(req, [&] { return dts::storage::http::page(store_.catalog(
                dts::storage::http::parameter(req,"after_id"), dts::storage::http::limit(req))); });
        });
        CROW_ROUTE(app_, "/api/storage/history")([this](const crow::request& req) {
            return guarded(req, [&] { return dts::storage::http::page(store_.history(
                dts::storage::http::parameter(req,"series_id"),dts::storage::http::parameter(req,"after_id"),
                dts::storage::http::parameter(req,"through_id"),dts::storage::http::limit(req),
                dts::storage::http::parameter(req,"from_ms"),dts::storage::http::parameter(req,"to_ms",253402300799999LL))); });
        });
        CROW_ROUTE(app_, "/api/storage/backup").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req, [&] {
                if(!req.body.empty() && req.body!="{}")throw std::invalid_argument("Backup accepts no path or options in the request");
                // Do not block recording behind a full-database backup while
                // native event queues are filling. Shutdown uses the same order.
                std::lock_guard<std::mutex> lock(broker_mutex_);
                if(broker_.state()==dts::ConnectionState::Ready || broker_.state()==dts::ConnectionState::Connecting)
                    throw std::logic_error("Disconnect the broker before making a manual backup");
                Json j;j["backup"]=store_.backup();j["consistent_snapshot"]=true;return j;
            });
        });

        CROW_ROUTE(app_, "/api/build")([this](const crow::request& req) {
            return guarded(req, [&] { return dts::pricing::sensitivity_http::build_json(); });
        });
        CROW_ROUTE(app_, "/api/greeks/run").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req, [&] {
                const auto request = dts::pricing::sensitivity_http::parse_greeks(object(req));
                std::unique_lock<std::mutex> lock(pricing_mutex_, std::try_to_lock);
                if (!lock.owns_lock()) throw std::length_error("Research calculation busy; try again explicitly later");
                return dts::pricing::sensitivity_http::run(request);
            });
        });
        CROW_ROUTE(app_, "/api/scenarios/run").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req, [&] {
                const auto request = dts::pricing::sensitivity_http::parse_scenarios(object(req));
                std::unique_lock<std::mutex> lock(pricing_mutex_, std::try_to_lock);
                if (!lock.owns_lock()) throw std::length_error("Research calculation busy; try again explicitly later");
                return dts::pricing::sensitivity_http::run(request);
            });
        });
        CROW_ROUTE(app_, "/api/pricing/run").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req, [&] {
                const auto request = dts::pricing::http::parse(object(req));
                std::unique_lock<std::mutex> lock(pricing_mutex_, std::try_to_lock);
                if (!lock.owns_lock()) throw std::length_error("Pricing is busy; wait for the current experiment");
                return dts::pricing::http::record(request, dts::pricing::simulate(request.inputs, request.config));
            });
        });
        dts::dashboard::mount(app_, port_);
        CROW_ROUTE(app_, "/health")([this] {
            std::lock_guard<std::mutex> lock(broker_mutex_);
            Json j; j["status"] = worker_failed_ || store_.status().failed ? "degraded" : "ok";
            j["db_connected"] = assets_.available(); j["read_only"] = true; j["recording_healthy"] = !store_.status().failed;
            return response(std::move(j));
        });
        CROW_ROUTE(app_, "/api/dashboard")([this](const crow::request& req) {
            return guarded(req, [&] {
                std::lock_guard<std::mutex> lock(broker_mutex_);
                // Drain first so all displayed views share a coherent service state.
                broker_.poll();
                Json j; j["broker"] = status();
                j["build"] = dts::pricing::sensitivity_http::build_json();
                j["storage"] = dts::storage::http::status(store_.status());
                j["session_id"] = instance_ + "-" + std::to_string(broker_.generation());
                j["positions"] = positions_json(broker_.positions(), mode_ == "mock");
                std::vector<Json> subscriptions;
                const auto now = dts::Clock::now();
                for (const auto& entry : broker_.subscriptions()) {
                    Json row; row["subscription_id"] = entry.first;
                    row["contract"] = contract_json(broker_.contract(entry.second));
                    try { row["quote"] = quote_json(broker_.quote(entry.second), now); }
                    catch (const std::out_of_range&) { row["quote"] = nullptr; }
                    subscriptions.push_back(std::move(row));
                }
                j["subscriptions"] = std::move(subscriptions); return j;
            });
        });
        CROW_ROUTE(app_, "/echo").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req, [&] { Json j; j["body"] = req.body; return j; });
        });
        CROW_ROUTE(app_, "/api/assets")([this](const crow::request& req) {
            return guarded(req, [&] { std::lock_guard<std::mutex> lock(database_mutex_); return assets_.query(req); });
        });
        CROW_ROUTE(app_, "/ib/send").methods(crow::HTTPMethod::POST)([this] { return message(410, "Raw broker forwarding is disabled"); });
        CROW_ROUTE(app_, "/ib/status")([this](const crow::request& req) {
            return guarded(req, [&] { std::lock_guard<std::mutex> lock(broker_mutex_); return status(); });
        });
        CROW_ROUTE(app_, "/api/broker/connect").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req, [&] { std::lock_guard<std::mutex> lock(broker_mutex_); broker_.connect(); worker_failed_ = false; return status(); });
        });
        CROW_ROUTE(app_, "/api/broker/disconnect").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req, [&] { std::lock_guard<std::mutex> lock(broker_mutex_); broker_.disconnect(); return status(); });
        });
        CROW_ROUTE(app_, "/api/contracts/resolve").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req, [&] {
                const auto j = object(req); dts::ContractQuery query;
                query.symbol = text(j, "symbol"); query.exchange = text(j, "exchange", "SMART");
                query.currency = text(j, "currency", "USD"); query.primary_exchange = text(j, "primary_exchange");
                const auto type = text(j, "security_type", "STK");
                if (type == "OPT") {
                    query.security_type = dts::SecurityType::Option; dts::OptionTerms terms;
                    const auto right = text(j, "right");
                    if (right != "C" && right != "P") throw std::invalid_argument("Option right must be C or P");
                    terms.right = right == "C" ? dts::OptionRight::Call : dts::OptionRight::Put;
                    terms.strike = number(j, "strike"); terms.expiry = text(j, "expiry"); query.option = terms;
                } else if (type != "STK") throw std::invalid_argument("Only STK and OPT are supported");
                query.validate(); std::lock_guard<std::mutex> lock(broker_mutex_);
                Json out; out["request_id"] = broker_.resolve(query); out["status"] = "pending"; return out;
            });
        });
        CROW_ROUTE(app_, "/api/contracts/requests/<int>")([this](const crow::request& req, int id) {
            return guarded(req, [&] {
                if (id <= 0) throw std::invalid_argument("Invalid request ID");
                std::lock_guard<std::mutex> lock(broker_mutex_); const auto& view = broker_.resolution(id);
                Json j; j["status"] = snapshot_name(view.status); std::vector<Json> contracts;
                if (view.status == dts::SnapshotStatus::Complete)
                    for (const auto& c : view.contracts) contracts.push_back(contract_json(c));
                j["contracts"] = std::move(contracts); return j;
            });
        });
        CROW_ROUTE(app_, "/api/subscriptions").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req, [&] {
                const auto j = object(req); const auto id = number(j, "contract_id");
                if (id <= 0 || id > 2147483647.0 || std::floor(id) != id) throw std::invalid_argument("Invalid conId");
                std::lock_guard<std::mutex> lock(broker_mutex_); Json out;
                out["subscription_id"] = broker_.subscribe(static_cast<dts::ContractId>(id)); return out;
            });
        });
        CROW_ROUTE(app_, "/api/subscriptions/<int>").methods(crow::HTTPMethod::DELETE)([this](const crow::request& req, int id) {
            return guarded(req, [&] {
                if (id <= 0) throw std::invalid_argument("Invalid subscription ID");
                std::lock_guard<std::mutex> lock(broker_mutex_); Json j; j["cancelled"] = broker_.unsubscribe(id); return j;
            });
        });
        CROW_ROUTE(app_, "/api/quotes/<int>")([this](const crow::request& req, int id) {
            return guarded(req, [&] {
                std::lock_guard<std::mutex> lock(broker_mutex_);
                return quote_json(broker_.quote(id), dts::Clock::now());
            });
        });
        CROW_ROUTE(app_, "/api/positions/refresh").methods(crow::HTTPMethod::POST)([this](const crow::request& req) {
            return guarded(req, [&] {
                std::lock_guard<std::mutex> lock(broker_mutex_); Json j;
                j["request_id"] = broker_.request_positions(); j["status"] = "pending"; return j;
            });
        });
        CROW_ROUTE(app_, "/api/positions")([this](const crow::request& req) {
            return guarded(req, [&] {
                std::lock_guard<std::mutex> lock(broker_mutex_);
                return positions_json(broker_.positions(), mode_ == "mock");
            });
        });
    }
};
} // namespace
void startServer() { Application app; app.run(); }
