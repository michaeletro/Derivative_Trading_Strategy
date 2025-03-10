#include "../../headers/CrowRouteHandling/CrowRouteHandler.h"
#include <iostream>

// Constructor: Sets up all routes
CrowRouteHandler::CrowRouteHandler(crow::SimpleApp& app, DataBaseClass& db) : db_(db) {
    setupRoutes(app);
}

// Function to define API routes
void CrowRouteHandler::setupRoutes(crow::SimpleApp& app) {
    // 📌 Fetch all asset data
    CROW_ROUTE(app, "/api/asset_data")
    ([this]() {
        return fetchAllAssetData();
    });

    // 📌 Fetch asset data by ticker
    CROW_ROUTE(app, "/api/asset_data/<string>")
        .methods(crow::HTTPMethod::GET)
        ([this](const crow::request& req, crow::response& res, std::string ticker) {
            fetchAssetByTicker(req, res, ticker);
        });

    // 📌 Insert new asset data
    CROW_ROUTE(app, "/api/add_asset")
        .methods(crow::HTTPMethod::POST)
        ([this](const crow::request& req) {
            return insertAssetData(req);
        });

    // 📌 Update asset data
    CROW_ROUTE(app, "/api/update_asset")
        .methods(crow::HTTPMethod::PUT)
        ([this](const crow::request& req) {
            return updateAssetData(req);
        });

    // 📌 Delete asset data
    CROW_ROUTE(app, "/api/delete_asset/<string>")
        .methods(crow::HTTPMethod::DELETE)
        ([this](const crow::request& req, crow::response& res, std::string ticker) {
            deleteAsset(req, res, ticker);
        });
}

// 📌 Fetch all asset data
crow::json::wvalue CrowRouteHandler::fetchAllAssetData() {
    auto results = db_.fetchAssetData();
    crow::json::wvalue response;
    std::vector<crow::json::wvalue> asset_data_array;

    for (const auto& entry : results) {
        crow::json::wvalue asset_data;
        asset_data["id"] = entry.id;
        asset_data["ticker"] = entry.ticker;
        asset_data["date"] = entry.date;
        asset_data["open_price"] = entry.open_price;
        asset_data["close_price"] = entry.close_price;
        asset_data["high_price"] = entry.high_price;
        asset_data["low_price"] = entry.low_price;
        asset_data["volume"] = entry.volume;

        asset_data_array.push_back(std::move(asset_data));
    }

    response["asset_data"] = std::move(asset_data_array);
    return response;
}

// 📌 Fetch asset data by ticker
void CrowRouteHandler::fetchAssetByTicker(const crow::request&, crow::response& res, std::string ticker) {
    auto results = db_.queryAssetData(ticker);

    if (results.empty()) {
        res.code = 404;
        res.body = "❌ No data found for ticker: " + ticker;
        res.end();
        return;
    }

    crow::json::wvalue response;
    std::vector<crow::json::wvalue> asset_data_array;

    for (const auto& entry : results) {
        crow::json::wvalue asset_data;
        asset_data["ticker"] = entry.ticker;
        asset_data["date"] = entry.date;
        asset_data["open_price"] = entry.open_price;
        asset_data["close_price"] = entry.close_price;
        asset_data["high_price"] = entry.high_price;
        asset_data["low_price"] = entry.low_price;
        asset_data["volume"] = entry.volume;

        asset_data_array.push_back(std::move(asset_data));
    }

    response["asset_data"] = std::move(asset_data_array);
    res.write(response.dump());
    res.end();
}

// 📌 Insert new asset data
crow::response CrowRouteHandler::insertAssetData(const crow::request& req) {
    auto body = crow::json::load(req.body);
    if (!body) {
        return crow::response(400, "❌ Invalid JSON format.");
    }
    /*
    std::string ticker = body["ticker"].s();
    std::string date = body["date"].s();
    double open_price = body["open_price"].d();
    double close_price = body["close_price"].d();
    double high_price = body["high_price"].d();
    double low_price = body["low_price"].d();
    uint64_t volume = body["volume"].u();
    */
    bool success = false;
    //bool success = db_.addAssetData(ticker, date, open_price, close_price, high_price, low_price, volume);
    return success ? crow::response(200, "✅ Asset data added successfully.") 
                   : crow::response(500, "❌ Failed to insert asset data.");
}

// 📌 Update asset data

crow::response CrowRouteHandler::updateAssetData(const crow::request& req) {
    auto body = crow::json::load(req.body);
    if (!body) {
        return crow::response(400, "❌ Invalid JSON format.");
    }
    /*
    std::string ticker = body["ticker"].s();
    std::string date = body["date"].s();
    double open_price = body["open_price"].d();
    double close_price = body["close_price"].d();
    double high_price = body["high_price"].d();
    double low_price = body["low_price"].d();
    uint64_t volume = body["volume"].u();

    bool success = db_.updateAssetData(ticker, date, open_price, close_price, high_price, low_price, volume);
    */
   bool success = false;
    return success ? crow::response(200, "🔄 Asset data updated successfully.") 
                   : crow::response(500, "❌ Failed to update asset data.");
}

// 📌 Delete asset data
void CrowRouteHandler::deleteAsset(const crow::request&, crow::response& res, std::string ticker) {
    //bool success = db_.deleteAssetData(ticker);
    std::cout << "Deleting asset data for ticker: " << ticker << std::endl;
    // Simulate deletion
    bool success = false;
    if (success) {
        res.code = 200;
        res.body = "🗑️ Asset data deleted successfully.";
    } else {
        res.code = 500;
        res.body = "❌ Failed to delete asset data.";
    }
    res.end();
}
