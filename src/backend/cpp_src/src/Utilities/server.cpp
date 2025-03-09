#include "../../headers/Utilities/crow_all.h"
#include <csignal>
#include "../../headers/DataBase/DataBaseClass.h"

const std::string DB_FILE = "quant_data.db";
const std::string CSV_FILE = "backup_data.csv";

// ✅ Declare `db` globally (No need for capture)
DataBaseClass db(DB_FILE, CSV_FILE);  

// ✅ Graceful shutdown handler
void handleShutdown(int signum) {
    std::cout << "🔴 Server shutting down. Exporting data to CSV..." << std::endl;
    db.exportToCSV(CSV_FILE);
    exit(signum);
}

int main() {
    crow::SimpleApp app;

    // Route to fetch asset data
    CROW_ROUTE(app, "/api/asset_data")
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

    CROW_ROUTE(app, "/api/asset_data/<string>")
        .methods(crow::HTTPMethod::GET)
        ([&db](const crow::request& req, crow::response& res, std::string ticker) {
            auto results = db.queryAssetData(ticker);

            if (results.empty()) {
                res.code = 404;
                res.body = "No data found for ticker: " + ticker;
                res.end();
                return;
            }

                crow::json::wvalue response;
         
                // Create a JSON array to hold the asset data
                std::vector<crow::json::wvalue> asset_data_array;

            for (const auto& entry : results) {
                crow::json::wvalue asset_data;
                std::cout << entry[0] << " " << entry[1] << " " << entry[2] << " " << entry[3] << " " << entry[4] << " " << entry[5] << std::endl;
            }

            return 0;
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

    signal(SIGINT, handleShutdown); // Handle Ctrl+C
    app.port(8080).multithreaded().run();

    return 0;
}
