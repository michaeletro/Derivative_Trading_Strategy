#include <crow_all.h>
#include "DataBaseClass.h"

int main() {
    crow::SimpleApp app;
    DataBaseClass db("quant_data.db");

    // Route to fetch asset data
    CROW_ROUTE(app, "/api/asset_data").methods(crow::HTTPMethod::GET)
    ([&db]() {
        auto results = db.fetchAssetData();
        crow::json::wvalue response;
         
         // Create a JSON array to hold the asset data
         std::vector<crow::json::wvalue> asset_data_array;

        for(const auto& entry : results){
            crow::json::wvalue asset_data;
            asset_data["id"] = entry.id;
            asset_data["ticker"] = entry.ticker;
            asset_data["date"] = entry.date;
            asset_data["open_price"] = entry.open_price;
            asset_data["close_price"] = entry.close_price;
            asset_data["high_price"] = entry.high_price;
            asset_data["low_price"] = entry.low_price;
            asset_data["volume"] = entry.volume;

            // Add the asset data to the array
            asset_data_array.push_back(std::move(asset_data));
        }
        response["asset_data"] = std::move(asset_data_array);

        return response;
    });

    // Route to insert new asset data
    CROW_ROUTE(app, "/api/add_asset")
    .methods(crow::HTTPMethod::POST)
    ([&db](const crow::request& req) {
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

        if (success) {
            return crow::response(200, "Asset data added successfully.");
        } else {
            return crow::response(500, "Failed to insert asset data.");
        }
    });

    // Start the server
    app.port(8080).multithreaded().run();
}
