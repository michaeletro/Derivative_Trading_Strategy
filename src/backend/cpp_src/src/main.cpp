#include <iostream>
#include "../headers/APIConnection/APIConnectionClass.h"

int main() {
    // ✅ Create QueryData object
    // ✅ Create a `QueryData` instance with real API parameters
    QueryData query = {
        QueryAsset::STOCKS,           // Asset jmghn Type
        RestAPIQueryType::AGGREGATES,     // API Query Type
        WebSocketQueryType::NONE,     // WebSocket Query Type (not used)
        "https://api.polygon.io",    // Base API URL
        "2024-01-01", "2024-02-01",   // Date range
        "day", "asc",                 // Time span & sorting
        "NVDA",                     // Asset name
        "",                             // Ticker name (not used here)
        "",                             // Date of interest (optional)
        "",                 // API Key
        5000, 1,                        // Query limits
        "true", true,
        100.00, "AAPL"                      // Adjusted flag & Debug mode
    };

    APIConnection apiConn(query);  // ✅ Pass QueryData into APIConnection

    try {
        // ✅ Fetch and parse real API data
        rapidjson::Document doc = apiConn.fetchAPIData();

        // ✅ Print response count
        std::cout << "✅ Successfully retrieved " << doc["resultsCount"].GetInt() << " results!" << std::endl;
    } catch (const std::exception& e) {
        std::cerr << "❌ Error: " << e.what() << std::endl;
    }

    return 0;
}
