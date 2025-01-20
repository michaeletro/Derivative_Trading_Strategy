#ifndef TIMESERIESCLASS_H
#define TIMESERIESCLASS_H

#include "APIConnectionClass.h"
#include <vector>
#include <memory>

struct APIResult {
    double volume;
    double volume_weighted_price;
    double open_price;
    double close_price;
    double highest_price;
    double lowest_price;
    long long timestamp;
    int num_transactions;
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

        rapidjson::Value::Array fetchTimeSeriesData() const; // Fetches and parses the data
        void printTimeSeriesData() const; // Prints the data to the console
        void createNewCSV(const std::string& filename) const;
        void writeToCSV(const std::string& filename) const; // Exports data to CSV
};

#endif // TIMESERIESCLASS_H
