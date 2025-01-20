#include "StockParser.h"
#include <iostream>
#include "../libs/rapidjson/include/rapidjson/document.h"
#include "../libs/rapidjson/include/rapidjson/error/en.h"

void StockParser::parseFromJSON(const std::string& jsonString) {
    rapidjson::Document document;
    if (document.Parse(jsonString.c_str()).HasParseError()) {
        throw std::runtime_error("Error parsing JSON: " +
                                 std::string(rapidjson::GetParseError_En(document.GetParseError())));
    }

    // Extract top-level fields
    if (document.HasMember("ticker") && document["ticker"].IsString()) {
        ticker = document["ticker"].GetString();
    }

    if (document.HasMember("adjusted") && document["adjusted"].IsBool()) {
        adjusted = document["adjusted"].GetBool();
    }

    if (document.HasMember("status") && document["status"].IsString()) {
        status = document["status"].GetString();
    }

    // Extract results array
    if (document.HasMember("results") && document["results"].IsArray()) {
        for (const auto& result : document["results"].GetArray()) {
            StockData data = {
                result["v"].GetDouble(),
                result["vw"].GetDouble(),
                result["o"].GetDouble(),
                result["c"].GetDouble(),
                result["h"].GetDouble(),
                result["l"].GetDouble(),
                result["t"].GetInt64(),
                result["n"].GetInt()
            };
            results.push_back(data);
        }
    }
}

void StockParser::print() const {
    std::cout << "Ticker: " << ticker << "\n";
    std::cout << "Adjusted: " << (adjusted ? "True" : "False") << "\n";
    std::cout << "Status: " << status << "\n";
    std::cout << "Results:\n";
    for (const auto& result : results) {
        std::cout << " - Volume: " << result.volume
                  << ", VWAP: " << result.vw_price
                  << ", Open: " << result.open_price
                  << ", Close: " << result.close_price
                  << ", High: " << result.high_price
                  << ", Low: " << result.low_price
                  << ", Timestamp: " << result.timestamp
                  << ", Trades: " << result.trades << "\n";
    }
}
