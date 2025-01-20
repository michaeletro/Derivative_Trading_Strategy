#include "AssetClass.h"
#include <fstream>
#include <iostream>
#include <filesystem>
#include <stdexcept>

namespace fs = std::filesystem;

// Constructor
AssetClass::AssetClass(const std::string& asset_name, const std::string& start_date,
                       const std::string& end_date, const std::string& time_multiplier,
                       const std::string& time_span, const std::string& sort,
                       const std::string& api_key, int limit, bool adjusted, bool debug)
    : TimeSeriesClass(asset_name, start_date, end_date, time_multiplier, time_span, sort, 
                      api_key, limit, adjusted, debug) {}

// Validate Response
void AssetClass::validateResponse(const rapidjson::Document& response) const {
    TimeSeriesClass::validateResponse(response);

    if (response.HasMember("ticker") && response["ticker"].IsString() &&
        response["ticker"].GetString() != asset_name) {
        throw std::runtime_error("Invalid API response: Ticker does not match the requested asset name.");
    }

    if (debug) {
        std::cout << "AssetClass response validation passed successfully." << std::endl;
    }
}

// Fetch Asset Data
void AssetClass::fetchAssetData() {
    fetchTimeSeriesData();
    if (debug) {
        std::cout << "Asset data fetched successfully." << std::endl;
    }
}

// Write to CSV (Append or Create)
void AssetClass::writeToCSV(const std::string& filename) const {
    if (time_series_data.empty()) {
        throw std::runtime_error("Time series data is empty. Nothing to write.");
    }

    std::ofstream file(filename, std::ios::trunc);
    if (!file.is_open()) {
        throw std::runtime_error("Could not open file: " + filename);
    }

    // Write CSV header
    file << "Timestamp,Open_Price,Close_Price,Highest_Price,Lowest_Price,Volume,Volume_Weighted\n";

    // Write each data row
    for (const auto& entry : time_series_data) {
        file << entry.timestamp << ","
             << entry.open_price << ","
             << entry.close_price << ","
             << entry.highest_price << ","
             << entry.lowest_price << ","
             << entry.volume << ","
             << entry.volume_weighted_price << "\n";
    }

    file.close();
    std::cout << "Data successfully written to " << filename << std::endl;
}

// Create New CSV (Always Overwrite)
void AssetClass::createNewCSV(const std::string& file_name) const {
    // Delete the file if it exists
    if (fs::exists(file_name)) {
        fs::remove(file_name);
        if (debug) {
            std::cout << "Deleted existing file: " << file_name << std::endl;
        }
    }

    // Call writeToCSV to generate the new file
    writeToCSV(file_name);
    if (debug) {
        std::cout << "Created a new CSV file: " << file_name << std::endl;
    }
}
