#include "DatabaseClass.h"
#include <crow.h>

int main() {
    crow::SimpleApp app;

    // Initialize the database
    DatabaseClass db("quant_data.db");

    // Route to fetch asset data
    CROW_ROUTE(app, "/api/data").methods(crow::HTTPMethod::GET)([&db]() {
        auto results = db.fetchAssetData();
        crow::json::wvalue response;
        for (const auto& entry : results) {
            crow::json::wvalue json_entry;
            json_entry["id"] = entry.id;
            json_entry["ticker"] = entry.ticker;
            json_entry["date"] = entry.date;
            json_entry["open_price"] = entry.open_price;
            json_entry["close_price"] = entry.close_price;
            json_entry["high_price"] = entry.high_price;
            json_entry["low_price"] = entry.low_price;
            json_entry["volume"] = entry.volume;
            response["data"].push_back(json_entry);
        }
        return response;
    });

    // Route to add asset data
    CROW_ROUTE(app, "/api/add_data").methods(crow::HTTPMethod::POST)([&db](const crow::request& req) {
        auto body = crow::json::load(req.body);
        if (!body) {
            return crow::response(400, "Invalid JSON");
        }
        std::string ticker = body["ticker"].s();
        std::string date = body["date"].s();
        double open_price = body["open_price"].d();
        double close_price = body["close_price"].d();
        double high_price = body["high_price"].d();
        double low_price = body["low_price"].d();
        double volume = body["volume"].d();

        bool success = db.addAssetData(ticker, date, open_price, close_price, high_price, low_price, volume);
        return success ? crow::response(200, "Data added successfully") : crow::response(500, "Failed to add data");
    });

    app.port(8080).multithreaded().run();
}
