#include "../../headers/AssetClasses/AssetClass.h"
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

// Function to convert Unix ms timestamp to readable time
std::string convertUnixTimestampToTime(double unix_msec) {
    // Convert milliseconds to seconds
    std::time_t unix_sec = unix_msec / 1000;

    // Convert to system time
    std::tm* time_info = std::gmtime(&unix_sec); // For UTC
    // std::tm* time_info = std::localtime(&unix_sec); // For local time

    // Format the time into a readable string
    char buffer[100];
    std::strftime(buffer, sizeof(buffer), "%Y-%m-%d %H:%M:%S", time_info);

    return std::string(buffer);
}


std::vector<APIResult> AssetClass::fetchAssetData() {
    auto results = fetchTimeSeriesData(); // Assuming this returns a JSON array
    std::vector<APIResult> data;

    for (const auto& entry : results) {
        APIResult result{
            entry["timestamp"].GetInt64(),             // Store numeric timestamp
            entry["open_price"].GetDouble(),
            entry["close_price"].GetDouble(),
            entry["highest_price"].GetDouble(),
            entry["lowest_price"].GetDouble(),
            entry["volume"].GetDouble(),
            entry["volume_weighted_price"].GetDouble(),
            entry["num_transactions"].GetInt()
        };

        data.push_back(result);
        // Convert Unix timestamp to human-readable time
        result.date = convertUnixTimestampToTime(result.timestamp);

        // Debugging: Display converted timestamp for human readability
        std::cout << "Timestamp: "
                  << convertUnixTimestampToTime(result.timestamp) // For display
                  << ", Open: " << result.open_price
                  << ", Close: " << result.close_price << std::endl;
    }

    return data;
}

bool AssetClass::operator==(const AssetClass& other) const {
    return this->asset_name == other.asset_name;
}

std::string AssetClass::getAssetName() const {
    return asset_name;
}

double AssetClass::getCurrentValue() const {
    if (asset_data.empty()) {
        throw std::runtime_error("Time series data is empty.");
    }
    // Access the last APIResult struct in the vector
    const APIResult& last_entry = asset_data.back();
    return last_entry.volume_weighted_price; // Assuming "volume_weighted_price" is the field you need
}

// Write to CSV (Append or Create)
void AssetClass::writeToCSV(const std::string& filename) const {
    if (asset_data.empty()) {
        throw std::runtime_error("Time series data is empty. Nothing to write.");
    }

    std::ofstream file(filename, std::ios::trunc);
    if (!file.is_open()) {
        throw std::runtime_error("Could not open file: " + filename);
    }

    // Write CSV header
    file << "Timestamp,Open_Price,Close_Price,Highest_Price,Lowest_Price,Volume,Volume_Weighted\n";

    // Write each data row
    for (const auto& entry : asset_data) {
        file << entry.date << ","
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

void AssetClass::validateResponse(const rapidjson::Document& response) const {
    if (!response.HasMember("results") || !response["results"].IsArray()) {
        throw std::runtime_error("Invalid API response: 'results' field missing or not an array.");
    }
}