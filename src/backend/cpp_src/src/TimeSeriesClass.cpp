#include "TimeSeriesClass.h"
#include <iostream>
#include <fstream>
#include <stdexcept>

TimeSeriesClass::TimeSeriesClass(
    const std::string& asset_name,
    const std::string& start_date,
    const std::string& end_date,
    const std::string& time_multiplier,
    const std::string& time_span,
    const std::string& sort,
    const std::string& api_key,
    int limit,
    bool adjusted,
    bool debug)
    : APIConnection(asset_name, start_date, end_date, time_multiplier, time_span, sort, api_key, limit, adjusted, debug) {}

void TimeSeriesClass::validateResponse(const rapidjson::Document& response) const {
    if (!response.HasMember("results") || !response["results"].IsArray()) {
        throw std::runtime_error("Invalid JSON response: Missing or invalid 'results' array.");
    }
}

rapidjson::Value::Array TimeSeriesClass::fetchTimeSeriesData() {
    // Fetch API data using APIConnection
    rapidjson::Document response = fetchAPIData();

    // Validate response
    validateResponse(response);

    // Return the "results" array
    if (response.HasMember("results") && response["results"].IsArray()) {
        return response["results"].GetArray();
    } else {
        throw std::runtime_error("API response does not contain a valid 'results' field.");
    }
}

void TimeSeriesClass::printTimeSeriesData() const {
    for (const auto& entry : time_series_data) {
        std::cout << "Timestamp: " << entry.timestamp
                  << ", Open: " << entry.open_price
                  << ", Close: " << entry.close_price
                  << ", High: " << entry.highest_price
                  << ", Low: " << entry.lowest_price
                  << ", Volume: " << entry.volume
                  << ", VWAP: " << entry.volume_weighted_price
                  << std::endl;
    }
}

void TimeSeriesClass::writeToCSV(const std::string& filename) const {
    std::ofstream file(filename);
    if (!file.is_open()) {
        throw std::runtime_error("Unable to open file: " + filename);
    }

    file << "Timestamp,Open,Close,High,Low,Volume,VWAP,Transactions\n";
    for (const auto& record : time_series_data) {
        file << record.timestamp << ","
             << record.open_price << ","
             << record.close_price << ","
             << record.highest_price << ","
             << record.lowest_price << ","
             << record.volume << ","
             << record.volume_weighted_price << ","
             << record.num_transactions << "\n";
    }

    file.close();
}
