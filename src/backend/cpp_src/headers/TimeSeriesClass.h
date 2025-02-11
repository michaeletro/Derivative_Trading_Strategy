#ifndef TIMESERIESCLASS_H
#define TIMESERIESCLASS_H

#include "APIConnectionClass.h"
#include <vector>
#include <memory>

struct APIResult {
    int64_t timestamp = 0; // Numeric timestamp for calculations
    double open_price = 0.0;
    double close_price = 0.0;
    double highest_price = 0.0;
    double lowest_price = 0.0;
    double volume = 0.0;
    double volume_weighted_price = 0.0;
    int num_transactions = 0;
    std::string date = ""; // For display purposes
};

// TimeSeriesClass inherits from APIConnection
class TimeSeriesClass : public APIConnection {
    protected:
        std::vector<APIResult> time_series_data; // Parsed time-series data
    public:
        TimeSeriesClass(const std::string& asset_name, const std::string& start_date,
                        const std::string& end_date, const std::string& time_multiplier,
                        const std::string& time_span, const std::string& sort,
                        const std::string& api_key, int limit, bool adjusted, bool debug);

        void validateResponse(const rapidjson::Document& response) const override;

        rapidjson::Value::Array fetchTimeSeriesData(); // Fetches and parses the data
        void printTimeSeriesData() const; // Prints the data to the console
        void createNewCSV(const std::string& filename) const;
        void writeToCSV(const std::string& filename) const; // Exports data to CSV
};

#endif // TIMESERIESCLASS_H
