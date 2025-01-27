#ifndef ASSET_CLASS_H
#define ASSET_CLASS_H

#include "TimeSeriesClass.h"
#include <string>
#include <filesystem>
#include <vector>


class AssetClass : public TimeSeriesClass {
    protected:
        std::vector<APIResult> asset_data; // Store time series data

    public:
        AssetClass(const std::string& asset_name, const std::string& start_date = "2023-01-09",
                const std::string& end_date = "", const std::string& time_multiplier = "1",
                const std::string& time_span = "day", const std::string& sort = "asc",
                const std::string& api_key = "YOUR_API_KEY", int limit = 5000,
                bool adjusted = true, bool debug = true);
        virtual ~AssetClass() = default;

        std::vector<APIResult> fetchAssetData();
        void validateResponse(const rapidjson::Document& response) const override;

        // Operations on Assets
        std::string getAssetName() const; // Returns the name of the asset
        double getCurrentValue() const; // Calculates the current value of the asset
        bool operator==(const AssetClass& other) const; // Compare two AssetClass objects for

        // Methods for handling CSV files
        void writeToCSV(const std::string& file_name) const;   // Append to an existing file or create if it doesn't exist
        void createNewCSV(const std::string& file_name) const; // Always create a new file
};

#endif // ASSET_CLASS_H
