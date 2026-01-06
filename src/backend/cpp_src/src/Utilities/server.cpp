#include <csignal>
#include <cstdlib>
#include <filesystem>
#include <memory>
#include <algorithm>
#include <string>

// Tell Crow to use Boost.Asio (required because we depend on boost::asio elsewhere).
#define CROW_USE_BOOST 1
#include <boost/version.hpp>
#include <boost/asio.hpp>
#include <boost/asio/ssl.hpp>

// Ensure unqualified asio resolves to boost::asio for Crow and websocketpp single-headers.
namespace asio = boost::asio;

#include "../../headers/Utilities/crow_all.h"
#include "../../headers/DataBase/DataBaseClass.h"
#include "../../headers/APIConnection/IBWebSocket.h"

// Minimal Crow server with health, echo, DB-backed asset query, and optional IB relay.
namespace {

crow::SimpleApp app;
std::unique_ptr<DataBaseClass> g_db;
std::unique_ptr<IBWebSocket> g_ib;
std::string g_ibUrl;
bool g_ibEnabled = false;

bool parseBool(const std::string& val, bool defaultValue) {
    if (val.empty()) return defaultValue;
    if (val == "1" || val == "true" || val == "TRUE" || val == "yes" || val == "YES") return true;
    if (val == "0" || val == "false" || val == "FALSE" || val == "no" || val == "NO") return false;
    return defaultValue;
}

std::string envOrDefault(const char* key, const std::string& fallback) {
    const char* val = std::getenv(key);
    return val ? std::string(val) : fallback;
}

void handleShutdown(int signum) {
    std::cout << "Server shutting down (signal " << signum << ")" << std::endl;
    if (g_ib) {
        g_ib->close();
    }
    app.stop();
}

void tryInitDb() {
    const std::string dbPath = envOrDefault("DB_PATH", "quant_data.db");
    const std::string assetCsv = envOrDefault("ASSET_CSV", "backup_data.csv");
    const std::string stockCsv = envOrDefault("STOCK_CSV", "stock_backup.csv");
    const std::string optionCsv = envOrDefault("OPTION_CSV", "option_backup.csv");
    const bool createIfMissing = parseBool(envOrDefault("DB_CREATE_IF_MISSING", "true"), true);
    const bool restoreFromCsv = parseBool(envOrDefault("DB_RESTORE_FROM_CSV", "true"), true);
    const bool failFast = parseBool(envOrDefault("DB_FAIL_FAST", "true"), true);

    const bool exists = std::filesystem::exists(dbPath);
    if (!exists && !createIfMissing) {
        std::cerr << "❌ Database file not found at " << dbPath << "; creation disabled." << std::endl;
        if (failFast) {
            std::cerr << "⛔ DB_FAIL_FAST=true; exiting." << std::endl;
            std::exit(1);
        }
        return;
    }

    try {
        g_db = std::make_unique<DataBaseClass>(dbPath, assetCsv, stockCsv, optionCsv, createIfMissing, restoreFromCsv);
        std::cout << "✅ Database ready at " << dbPath << " (" << (exists ? "existing" : "new") << ")" << std::endl;
    } catch (const std::exception& ex) {
        std::cerr << "❌ Failed to init database: " << ex.what() << std::endl;
        if (failFast) {
            std::cerr << "⛔ DB_FAIL_FAST=true; exiting." << std::endl;
            std::exit(1);
        }
    }
}

crow::json::wvalue toJson(const std::vector<std::unique_ptr<AssetData>>& rows) {
    std::vector<crow::json::wvalue> arr;
    arr.reserve(rows.size());
    for (const auto& r : rows) {
        crow::json::wvalue item;
        item["id"] = r->id;
        item["ticker"] = r->ticker;
        item["open_price"] = r->open_price;
        item["close_price"] = r->close_price;
        item["high_price"] = r->high_price;
        item["low_price"] = r->low_price;
        item["volume"] = r->volume;
        item["date"] = r->date;
        arr.push_back(std::move(item));
    }
    crow::json::wvalue res;
    res["results"] = std::move(arr);
    res["count"] = static_cast<int>(res["results"].size());
    return res;
}

void tryInitIb() {
    const std::string enable = envOrDefault("ENABLE_IB_WS", "true");
    g_ibEnabled = parseBool(enable, true);
    if (!g_ibEnabled) {
        std::cout << "ℹ️ IB WebSocket disabled (set ENABLE_IB_WS=true to enable)." << std::endl;
        return;
    }

    g_ibUrl = envOrDefault("IB_WS_URL", "wss://localhost:5000/v1/api/ws");
    try {
        g_ib = std::make_unique<IBWebSocket>();
        g_ib->connect(g_ibUrl);
        std::cout << "✅ IB WebSocket connection attempt to " << g_ibUrl << std::endl;
    } catch (const std::exception& ex) {
        std::cerr << "❌ Failed to initialize IB WebSocket: " << ex.what() << std::endl;
        g_ibEnabled = false;
    }
}

} // namespace

void startServer() {
    tryInitDb();
    tryInitIb();

    CROW_ROUTE(app, "/health").methods(crow::HTTPMethod::GET)([] {
        crow::json::wvalue res;
        res["status"] = "ok";
        res["db_connected"] = g_db != nullptr;
        return crow::response{res};
    });

    CROW_ROUTE(app, "/echo").methods(crow::HTTPMethod::POST)([](const crow::request& req) {
        return crow::response{req.body};
    });

    CROW_ROUTE(app, "/api/assets").methods(crow::HTTPMethod::GET)([](const crow::request& req) {
        if (!g_db) {
            return crow::response{503, "Database unavailable"};
        }

        const char* ticker = req.url_params.get("ticker");
        const char* start = req.url_params.get("start");
        const char* end = req.url_params.get("end");
        const char* limitStr = req.url_params.get("limit");
        int limit = 100;
        if (limitStr) {
            try { limit = std::stoi(limitStr); } catch (...) { limit = 100; }
            limit = std::max(1, std::min(limit, 5000));
        }

        auto rows = g_db->queryData(
            "asset_data",
            ticker ? ticker : "",
            start ? start : "",
            end ? end : "",
            limit,
            true);

        return crow::response{toJson(rows)};
    });

    CROW_ROUTE(app, "/ib/status").methods(crow::HTTPMethod::GET)([] {
        crow::json::wvalue res;
        res["enabled"] = g_ibEnabled;
        res["connected"] = g_ib ? g_ib->isConnected() : false;
        res["url"] = g_ibUrl;
        return crow::response{res};
    });

    CROW_ROUTE(app, "/ib/send").methods(crow::HTTPMethod::POST)([](const crow::request& req) {
        if (!g_ibEnabled || !g_ib) {
            return crow::response{503, "IB WebSocket disabled"};
        }
        if (!g_ib->isConnected()) {
            return crow::response{503, "IB WebSocket not connected"};
        }
        const std::string payload = req.body;
        if (payload.empty()) {
            return crow::response{400, "Empty body"};
        }
        g_ib->sendMessage(payload);
        crow::json::wvalue res;
        res["sent_bytes"] = static_cast<int>(payload.size());
        return crow::response{res};
    });

    std::signal(SIGINT, handleShutdown);
    std::signal(SIGTERM, handleShutdown);

    constexpr uint16_t port = 8080;
    std::cout << "Starting HTTP server on :" << port << std::endl;
    app.port(port).multithreaded().run();
}
