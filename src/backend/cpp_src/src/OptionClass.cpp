#include "OptionClass.h"
#include <iostream>

// Constructor
OptionClass::OptionClass(const std::string& asset_name, const std::string& start_date,
                         const std::string& end_date, const std::string& time_multiplier,
                         const std::string& time_span, const std::string& sort,
                         const std::string& api_key, int limit, bool adjusted, bool debug)
    : AssetClass(asset_name, start_date, end_date, time_multiplier, time_span, sort, api_key,
                 limit, adjusted, debug) {}

// Validate Parameters
void OptionClass::validateParameters() const {
    AssetClass::validateParameters(); // Call parent validation

    // Validate that asset_name has the "O:" prefix for options
    if (asset_name.find("O:") != 0) {
        throw std::invalid_argument("Invalid asset name for OptionClass. Expected prefix 'O:'.");
    }

    if (debug) {
        std::cout << "OptionClass parameters validated successfully." << std::endl;
    }
}

// Fetch Option Data
void OptionClass::fetchOptionData() {
    fetchAssetData(); // Reuse AssetClass fetch logic
    if (debug) {
        std::cout << "Option data fetched successfully." << std::endl;
    }
}
