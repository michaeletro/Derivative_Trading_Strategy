#include "../../headers/AssetClasses/StockClass.h"
#include <iostream>

// Constructor
StockClass::StockClass(const std::string& asset_name, const std::string& start_date,
                       const std::string& end_date, const std::string& time_multiplier,
                       const std::string& time_span, const std::string& sort,
                       const std::string& api_key, int limit, bool adjusted, bool debug)
    : AssetClass(asset_name, start_date, end_date, time_multiplier, time_span, sort, api_key,
                 limit, adjusted, debug) {}

// Fetch Stock Data
void StockClass::fetchStockData() {
    fetchAssetData(); // Reuse AssetClass fetch logic
    if (debug) {
        std::cout << "Stock data fetched successfully." << std::endl;
    }
}
