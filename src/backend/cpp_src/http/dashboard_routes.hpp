#pragma once
#include <crow_all.h>
#include <dts_dashboard_assets.hpp>
#include <string>

namespace dts::dashboard {
// An explicit in-binary allowlist: no runtime filesystem traversal or templates.
inline void mount(crow::SimpleApp& app, int port) {
    const auto serve = [port](const crow::request& req, const char* body, const char* mime) {
        const auto host = req.get_header_value("Host");
        if (host != "127.0.0.1:" + std::to_string(port) && host != "localhost:" + std::to_string(port))
            return crow::response(403, "Invalid Host header");
        const auto origin = req.get_header_value("Origin");
        if (!origin.empty() && origin != "http://" + host)
            return crow::response(403, "Cross-origin access denied");
        crow::response response(body);
        response.set_header("Content-Type", mime);
        response.set_header("Cache-Control", "no-store");
        response.set_header("X-Content-Type-Options", "nosniff");
        response.set_header("Referrer-Policy", "no-referrer");
        response.set_header("Cross-Origin-Resource-Policy", "same-origin");
        response.set_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'; worker-src 'none'");
        return response;
    };
    CROW_ROUTE(app, "/")([serve](const crow::request& req) { return serve(req, assets::index_html, "text/html; charset=utf-8"); });
    // This Crow version registers /dashboard as a redirect for /dashboard/.
    // Registering both explicitly makes startup fail with a duplicate handler.
    CROW_ROUTE(app, "/dashboard/")([serve](const crow::request& req) { return serve(req, assets::index_html, "text/html; charset=utf-8"); });
    CROW_ROUTE(app, "/dashboard/<string>")([serve](const crow::request& req, const std::string& name) {
        if (name == "index.html") return serve(req, assets::index_html, "text/html; charset=utf-8");
        if (name == "dashboard.css") return serve(req, assets::dashboard_css, "text/css; charset=utf-8");
        if (name == "app.mjs") return serve(req, assets::app_mjs, "text/javascript; charset=utf-8");
        if (name == "model.mjs") return serve(req, assets::model_mjs, "text/javascript; charset=utf-8");
        if (name == "pricing.mjs") return serve(req, assets::pricing_mjs, "text/javascript; charset=utf-8");
        if (name == "pricing-model.mjs") return serve(req, assets::pricing_model_mjs, "text/javascript; charset=utf-8");
        return crow::response(404, "Unknown dashboard asset");
    });
}
} // namespace dts::dashboard
