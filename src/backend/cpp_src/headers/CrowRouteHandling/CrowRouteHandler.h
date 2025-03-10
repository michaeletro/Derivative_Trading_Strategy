#pragma once

#include "../Utilities/crow_all.h"
#include "../DataBase/DataBaseClass.h" // Include your database class

class CrowRouteHandler {
public:
    explicit CrowRouteHandler(crow::SimpleApp& app, DataBaseClass& db);

private:
    DataBaseClass& db_;
    
    void setupRoutes(crow::SimpleApp& app);

    // Route Handlers
    crow::json::wvalue fetchAllAssetData();
    void fetchAssetByTicker(const crow::request& req, crow::response& res, std::string ticker);
    crow::response insertAssetData(const crow::request& req);
    crow::response updateAssetData(const crow::request& req);
    void deleteAsset(const crow::request& req, crow::response& res, std::string ticker);
};
