#ifndef TIMESERIESCLASS_H
#define TIMESERIESCLASS_H

#include "APIConnectionClass.h"
#include <vector>
#include <string>

// TimeSeriesClass inherits from APIConnection
class TimeSeriesClass : public APIConnection {
    protected:
        std::vector<APIResult> time_series_data; // Store time series data
    public:
        // Constructor
        TimeSeriesClass(const std::string& asset_name, const std::string& start_date = "2023-01-01",
                        const std::string& end_date = "2023-01-10", const std::string& time_multiplier = "1",
                        const std::string& time_span = "day", const std::string& sort = "asc",
                        const std::string& api_key = "", int limit = 5000, bool adjusted = true, bool debug = true);

        // Fetch time series data
        void fetchTimeSeriesData();

        // Print time series data
        void printTimeSeriesData() const;

        // Write time series data to a CSV file
        void writeToCSV(const std::string& filename) const;

        // Create a new CSV file
        void createNewCSV(const std::string& filename) const;
};

#endif // TIMESERIESCLASS_H
