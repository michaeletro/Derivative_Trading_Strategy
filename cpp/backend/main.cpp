#include "../libs/json/single_include/nlohmann/json.hpp"
#include <iostream>
#include "StockParser.h"
#include "APIConnection.h"

int main() {
    // Example JSON response
    try {
    std::string jsonResponseStr = R"({
        "ticker": "NVDA",
        "queryCount": 8,
        "resultsCount": 8,
        "adjusted": true,
        "results": [
            {"v": 180194359.0, "vw": 138.8311, "o": 138.97, "c": 138.81, "h": 139.95, "l": 137.13, "t": 1733720400000, "n": 1525627},
            {"v": 204088158.0, "vw": 137.0564, "o": 139.01, "c": 135.07, "h": 141.82, "l": 133.79, "t": 1733806800000, "n": 1646482}
        ],
        "status": "DELAYED",
        "request_id": "07e2c9b808547c93f3c0103ab23790ac",
        "count": 8
    })";

    // Parse JSON string
    nlohmann::json jsonResponse = nlohmann::json::parse(jsonResponseStr);

    // Process the response
    StockParser parser;
    parser.parseFromJSON(jsonResponse);
    parser.print();

    } catch (const std::exception& e) {
        std::cerr << "Error: " << e.what() << std::endl;
    }
    // Print parsed data

    return 0;
}
