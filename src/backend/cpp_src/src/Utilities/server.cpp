#define CROW_USE_BOOST 1
#include <boost/asio.hpp>
namespace asio = boost::asio;
#include <crow_all.h>
#include <dashboard_routes.hpp>
#include <sqlite3.h>
#include <dts/read_only_service.hpp>
#include <dts/mock_broker.hpp>
#ifdef DTS_WITH_IBKR
#include <dts/tws_broker.hpp>
#endif
#include <atomic>
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

// Existing asset_data schema, opened read-only. No implicit migrations or backups.
class AssetRepository {
    sqlite3* db_ = nullptr;
public:
    explicit AssetRepository(const std::string& path) {
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
        int status = SQLITE_OK;
        while ((status = sqlite3_step(raw)) == SQLITE_ROW) {
            Json row; row["id"] = static_cast<std::int64_t>(sqlite3_column_int64(raw, 0));
            for (const auto column : {1, 7}) {
                const auto* value = sqlite3_column_text(raw, column);
                row[column == 1 ? "ticker" : "date"] = value ? reinterpret_cast<const char*>(value) : "";
            }
            const char* prices[] = {"open_price", "close_price", "high_price", "low_price"};
            for (int i = 0; i < 4; ++i) row[prices[i]] = sqlite3_column_double(raw, i + 2);
            row["volume"] = static_cast<std::int64_t>(sqlite3_column_int64(raw, 6));
            rows.push_back(std::move(row));
        }
        if (status != SQLITE_DONE) throw std::runtime_error("Asset query did not complete");
        Json result; result["count"] = rows.size(); result["results"] = std::move(rows); return result;
    }
};

class Application {
public:
    Application() : mode_(env("DTS_BROKER", "none")), token_(env("DTS_API_TOKEN")),
        port_(integer(env("HTTP_PORT", "8080"), 1024, 65535)),
        assets_(env("DB_PATH", "quant_data.db")), broker_(make_broker()) {
        if (env("ENABLE_IB_WS", "false") != "false" && env("ENABLE_IB_WS") != "0")
            throw std::invalid_argument("Client Portal relay is retired; configure DTS_BROKER instead");
        if (env("DB_FAIL_FAST", "false") == "true" && !assets_.available())
            throw std::runtime_error("Configured asset database unavailable");
        routes();
    }
    ~Application() { stop(); }
    void run() {
        worker_ = std::thread([this] {
            std::unique_lock<std::mutex> lock(broker_mutex_);
            while (!stopping_) {
                try { broker_.poll(); }
                catch (...) { broker_.disconnect(); worker_failed_ = true; }
                wake_.wait_for(lock, std::chrono::milliseconds(10), [this] { return stopping_; });
            }
            broker_.disconnect();
        });
        app_.bindaddr("127.0.0.1").port(static_cast<std::uint16_t>(port_)).concurrency(2).run();
        stop();
    }
private:
    crow::SimpleApp app_;
    std::string mode_, token_;
    int port_;
    AssetRepository assets_;
    dts::ReadOnlyService broker_;
    // Non-secret instance identifier prevents charts joining observations across restarts.
    const std::string instance_ = std::to_string(std::chrono::system_clock::now().time_since_epoch().count());
    std::mutex broker_mutex_, database_mutex_;
    std::condition_variable wake_;
    std::thread worker_;
    bool stopping_ = false, worker_failed_ = false;
    std::unique_ptr<dts::IBroker> make_broker() {
        if (mode_ == "none") return {};
        if (mode_ == "mock") return std::make_unique<dts::MockBroker>();
        if (mode_ != "tws") throw std::invalid_argument("DTS_BROKER must be none, mock, or tws");
        if (token_.size() < 24) throw std::invalid_argument("TWS mode requires DTS_API_TOKEN of at least 24 characters");
#ifdef DTS_WITH_IBKR
        dts::TwsConfig config;
        config.host = env("IB_HOST", "127.0.0.1");
        config.port = integer(env("IB_PORT", "4002"), 1, 65535);
        config.client_id = integer(env("IB_CLIENT_ID", "17"), 1, 2147483647);
        config.market_data_type = integer(env("IB_MARKET_DATA_TYPE", "3"), 1, 4);
        config.timeout = std::chrono::milliseconds(integer(env("IB_TIMEOUT_MS", "10000"), 100, 60000));
        return std::make_unique<dts::TwsBroker>(config);
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
        result.set_header("X-Content-Type-Options", "nosniff"); return result;
    }
    crow::response message(int status, const char* text_value) const {
        Json j; j["error"] = text_value; return response(std::move(j), status);
    }
    template<class Function> crow::response guarded(const crow::request& req, Function fn) {
        const auto host = req.get_header_value("Host");
        if (host != "127.0.0.1:" + std::to_string(port_) && host != "localhost:" + std::to_string(port_))
            return message(403, "Invalid Host header");
        const auto origin = req.get_header_value("Origin");
        if (!origin.empty() && origin != "http://" + host) return message(403, "Cross-origin access denied");
        if (!token_.empty() && req.get_header_value("Authorization") != "Bearer " + token_)
            return message(401, "Bearer token required");
        if (req.body.size() > 8192) return message(413, "Request too large");
        try { return response(fn()); }
        catch (const std::out_of_range&) { return message(404, "Unknown resource or numeric value outside range"); }
        catch (const std::invalid_argument& e) { return message(400, e.what()); }
        catch (const std::length_error& e) { return message(429, e.what()); }
        catch (const std::logic_error& e) { return message(409, e.what()); }
        catch (...) { return message(503, "Service operation unavailable"); }
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
        dts::dashboard::mount(app_, port_);
        CROW_ROUTE(app_, "/health")([this] {
            std::lock_guard<std::mutex> lock(broker_mutex_);
            Json j; j["status"] = worker_failed_ ? "degraded" : "ok";
            j["db_connected"] = assets_.available(); j["read_only"] = true;
            return response(std::move(j));
        });
        CROW_ROUTE(app_, "/api/dashboard")([this](const crow::request& req) {
            return guarded(req, [&] {
                std::lock_guard<std::mutex> lock(broker_mutex_);
                // Drain first so all displayed views share a coherent service state.
                broker_.poll();
                Json j; j["broker"] = status();
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
