#include "crow_all.h"
#include "AssetClass.h"
#include <vector>
#include <memory>

#include <iostream>
#include <fstream>


/*
struct APIResult {
    int64_t timestamp; // Numeric timestamp for calculations
    double open_price;
    double close_price;
    double highest_price;
    double lowest_price;
    double volume;
    double volume_weighted_price;
    int num_transactions;
    std::string date; // For display purposes
};
*/
std::vector<std::vector<std::string>> readCSV(const std::string& filename) {
    std::ifstream file(filename);
    if (!file.is_open()) {
        throw std::runtime_error("Could not open file: " + filename);
    }

    std::vector<std::vector<std::string>> data;
    std::string line;

    while (std::getline(file, line)) {
        std::vector<std::string> row;
        std::istringstream stream(line);
        std::string cell;

        while (std::getline(stream, cell, ',')) {
            row.push_back(cell);
        }
        data.push_back(row);
    }

    file.close();
    return data;
}


int main() {
    crow::SimpleApp app;
    const std::string api_key = readCSV("../data_files/access_key.csv")[0][0];

    // Create a StockClass object
    auto asset = std::make_shared<AssetClass>("MSFT", "2024-06-01", "2025-01-10", 
                                                "100", "day", "asc", api_key, true, true);
    //std::vector<APIResult> processed_data = asset->fetchAssetData();

    // Define the route
    CROW_ROUTE(app, "/api/data")
    ([&](const crow::request&) {
        crow::json::wvalue result;
        crow::json::wvalue::list data_list;

        for (const auto& entry : processed_data) {
            crow::json::wvalue json_entry;
            json_entry["date"] = entry.timestamp;
            json_entry["open_price"] = entry.open_price;
            json_entry["close_price"] = entry.close_price;
            json_entry["highest_price"] = entry.highest_price;
            json_entry["lowest_price"] = entry.lowest_price;
            json_entry["volume"] = entry.volume;
            json_entry["volume_weighted_price"] = entry.volume_weighted_price;

            data_list.push_back(std::move(json_entry));
        }

        result["data"] = std::move(data_list);
        return result;
    });

        // Define the /api/data endpoint
    CROW_ROUTE(app, "/api/get-data")
        .methods(crow::HTTPMethod::GET)([]() {
            crow::json::wvalue result;
            result["data"] = "Hello from /api/data endpoint!";
            return result;
        });
    

    // Start the server
    app.port(8080).multithreaded().run();

    return 0;
}
