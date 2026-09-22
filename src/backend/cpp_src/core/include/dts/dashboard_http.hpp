#pragma once
#include <crow_all.h>
#include <dashboard_assets.hpp>
#include "read_only_service.hpp"

namespace dts::web {
using Json = crow::json::wvalue;
inline Json instrument_json(const Contract& c) {
    Json j;
    j["contract_id"] = c.id; j["symbol"] = c.symbol; j["exchange"] = c.exchange;
    j["currency"] = c.currency; j["multiplier"] = c.multiplier;
    j["security_type"] = c.security_type == SecurityType::Option ? "OPT" : "STK";
    if (c.option) {
        j["expiry"] = c.option->expiry; j["strike"] = c.option->strike;
        j["right"] = c.option->right == OptionRight::Call ? "C" : "P";
        j["exercise_style"] = "unknown";
    }
    return j;
}
inline Json quote_json(const Quote& quote, Clock::time_point now) {
    Json j; j["contract_id"] = quote.contract_id; j["indicative_only"] = true;
    switch (quote.data_type) {
        case MarketDataType::Realtime: j["data_type"] = "realtime"; break;
        case MarketDataType::Delayed: j["data_type"] = "delayed"; break;
        case MarketDataType::Frozen: j["data_type"] = "frozen"; break;
        case MarketDataType::DelayedFrozen: j["data_type"] = "delayed_frozen"; break;
        default: j["data_type"] = "simulation";
    }
    const auto mid = quote.mid(now, std::chrono::seconds(5));
    j["mid"] = mid ? Json(*mid) : Json(nullptr);
    const auto side = [now](const std::optional<QuoteSide>& q) {
        if (!q || !std::isfinite(q->price)) return Json(nullptr);
        Json result; result["price"] = q->price;
        result["receipt_age_ms"] = std::chrono::duration_cast<std::chrono::milliseconds>(now - q->received_at).count();
        return result;
    };
    j["bid"] = side(quote.bid); j["ask"] = side(quote.ask); return j;
}
// Caller holds the application broker mutex. Reads existing state only: no SDK
// requests, reconnects, snapshot refreshes, or synthetic prices from HTTP GET.
inline Json dashboard_json(const ReadOnlyService& service, Json broker, bool simulation) {
    Json result; result["schema_version"] = 1; result["broker"] = std::move(broker);
    std::vector<Json> subscriptions, positions;
    Json p; p["status"] = "unavailable"; p["positions"] = Json(nullptr);
    p["simulation"] = simulation; p["snapshot_not_stream"] = true;
    if (service.state() == ConnectionState::Ready) {
        const auto now = Clock::now();
        for (const auto& entry : service.subscriptions()) {
            Json sub; sub["subscription_id"] = entry.first;
            sub["contract"] = instrument_json(service.contract(entry.second));
            try { sub["quote"] = quote_json(service.quote(entry.second), now); }
            catch (const std::out_of_range&) { sub["quote"] = nullptr; }
            subscriptions.push_back(std::move(sub));
        }
        const auto& view = service.positions();
        if (view.status == SnapshotStatus::Pending) p["status"] = "pending";
        if (view.status == SnapshotStatus::Failed) p["status"] = "failed";
        if (view.status == SnapshotStatus::Complete && view.completed_at) {
            p["status"] = "complete";
            for (const auto& position : view.positions) {
                Json row; row["account"] = position.account; row["quantity"] = position.quantity;
                row["contract"] = instrument_json(position.contract); positions.push_back(std::move(row));
            }
            p["positions"] = std::move(positions);
            p["completed_at_unix_ms"] = std::chrono::duration_cast<std::chrono::milliseconds>(view.completed_at->time_since_epoch()).count();
        }
    }
    result["subscriptions"] = std::move(subscriptions); result["positions"] = std::move(p); return result;
}
inline void install_dashboard(crow::SimpleApp& app, int port) {
    const auto serve = [port](const crow::request& request, std::string_view body, const char* type) {
        const auto host = request.get_header_value("Host");
        if (host != "127.0.0.1:" + std::to_string(port) && host != "localhost:" + std::to_string(port))
            return crow::response(403);
        const auto origin = request.get_header_value("Origin");
        if (!origin.empty() && origin != "http://" + host) return crow::response(403);
        crow::response response{std::string(body)};
        response.set_header("Content-Type", type);
        response.set_header("Cache-Control", "no-store");
        response.set_header("X-Content-Type-Options", "nosniff");
        response.set_header("Referrer-Policy", "no-referrer");
        response.set_header("X-Frame-Options", "DENY");
        response.set_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'");
        return response;
    };
    CROW_ROUTE(app, "/")([serve](const crow::request& r) { return serve(r, html, "text/html; charset=utf-8"); });
    CROW_ROUTE(app, "/dashboard/")([serve](const crow::request& r) { return serve(r, html, "text/html; charset=utf-8"); });
    CROW_ROUTE(app, "/dashboard/styles.css")([serve](const crow::request& r) { return serve(r, css, "text/css; charset=utf-8"); });
    CROW_ROUTE(app, "/dashboard/app.mjs")([serve](const crow::request& r) { return serve(r, app_js, "text/javascript; charset=utf-8"); });
    CROW_ROUTE(app, "/dashboard/model.mjs")([serve](const crow::request& r) { return serve(r, model_js, "text/javascript; charset=utf-8"); });
}
} // namespace dts::web
