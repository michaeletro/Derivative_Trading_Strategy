#include "TimeSeriesClass.h"
#include <iostream>
#include <fstream>
#include <stdexcept>

// Constructor
TimeSeriesClass::TimeSeriesClass(const std::string& asset_name, const std::string& start_date,
                                 const std::string& end_date, const std::string& time_multiplier,
                                 const std::string& time_span, const std::string& sort,
                                 const std::string& api_key, int limit, bool adjusted, bool debug)
    : APIConnection(asset_name, start_date, end_date, time_multiplier, time_span, sort, api_key,
                    limit, adjusted, debug){}

// Fetch time series data
void TimeSeriesClass::fetchTimeSeriesData() {
    try {
        // Use APIConnection's fetchAPIData method to retrieve data
        time_series_data = fetchAPIData();

        if (debug) {
            std::cout << "Time series data fetched successfully. Number of records: "
                      << time_series_data.size() << std::endl;
        }
    } catch (const std::exception& e) {
        std::cerr << "Error fetching time series data: " << e.what() << std::endl;
    }
}

// Print time series data
void TimeSeriesClass::printTimeSeriesData() const {
    std::cout << "Time Series Data:" << std::endl;
    for (const auto& record : time_series_data) {
        std::cout << "Timestamp: " << record.timestamp
                  << ", Open: " << record.open_price
                  << ", Close: " << record.close_price
                  << ", High: " << record.highest_price
                  << ", Low: " << record.lowest_price
                  << ", Volume: " << record.volume
                  << ", VWAP: " << record.volume_weighted_price
                  << ", Transactions: " << record.num_transactions
                  << std::endl;
    }
}

// Write time series data to a CSV file
void TimeSeriesClass::writeToCSV(const std::string& filename) const {
    std::ofstream file(filename, std::ios::app); // Open in append mode
    if (!file.is_open()) {
        throw std::runtime_error("Failed to open file: " + filename);
    }

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
    std::cout << "Data written to " << filename << " successfully." << std::endl;
}

// Create a new CSV file
void TimeSeriesClass::createNewCSV(const std::string& filename) const {
    std::ofstream file(filename, std::ios::trunc); // Open in truncate mode (overwrite file)
    if (!file.is_open()) {
        throw std::runtime_error("Failed to open file: " + filename);
    }

    // Write headers
    file << "Timestamp,Open,Close,High,Low,Volume,VWAP,Transactions\n";

    // Write data
    writeToCSV(filename);

    file.close();
    std::cout << "New CSV file created: " << filename << std::endl;
}
