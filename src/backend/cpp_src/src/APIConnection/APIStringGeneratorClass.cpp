#include "../../headers/APIConnection/APIStringGeneratorClass.h"
#include <sstream>
#include <concepts>
#include <iostream>
#include <type_traits>


// ✅ Generate complete API URL based on `QueryData`
std::string APIStringGenerator::generateURL(const QueryData& query) {
    std::string endpoint_url;

    // ✅ Get API endpoint for the specified query type
    if (query.rest_api_query_type != RestAPIQueryType::NONE) {
        endpoint_url = APIStringGenerator::RestAPIQueryTypeGenerate(query);
    } else {
        throw std::invalid_argument("❌ Error: Invalid RestAPIQueryType!");
    }

    if (query.debug) {
        std::cout << "🔍 Generated URL: " << endpoint_url << std::endl;
    }

    return endpoint_url;
}

// ✅ Retrieve API endpoint string from `endpointMap`
std::string APIStringGenerator::RestAPIQueryTypeGenerate(const QueryData& query) {
    // String to work on
    std::ostringstream api_path_appended;
    api_path_appended << query.base_url << getEndPoint(query.rest_api_query_type);

    // This is to handle the Unique Case by Case API String Request
    switch(getEndPointIndex(query.rest_api_query_type)){
        // This is the Grouped Daily Request
        // Case 0, 2, 3, 4, 5, 8
        // Case 7 does not work for Currencies

        // Grouped Daily
        case 1:
            api_path_appended << APIStringGenerator::string_locale_append(query) << query.start_date << "?adjusted=" <<
            query.adjusted << "&apiKey=" << query.api_key;
        case 2:
            api_path_appended << APIStringGenerator::string_asset_append(query) << "/" << query.date_of_interest <<
             "?adjusted" << query.adjusted << "&apiKey=" << query.api_key;
        case 3:
            api_path_appended << APIStringGenerator::string_asset_append(query) << "/prev?adjusted" << query.adjusted
            << "&apiKey=" << query.api_key;
        case 4:
            api_path_appended << APIStringGenerator::string_asset_append(query) << "/limit=" << query.limit <<
             "&apiKey=" << query.api_key;
        case 5:
            api_path_appended << APIStringGenerator::string_asset_append(query) << "?apiKey=" << query.api_key;
        // Last Trade Crypto Pair
        case 6:
            api_path_appended << query.asset_name.substr(0,3) << "/" << query.asset_name.substr(3,3) << "?apiKey=" << query.api_key;
        case 7:
            api_path_appended << APIStringGenerator::string_asset_append(query) << "/limit=" << query.limit
            << "&apiKey=" << query.api_key;
        case 8:
            api_path_appended << APIStringGenerator::string_asset_append(query) << "?apiKey=" << query.api_key;
        // Last Quote Currency Pair
        case 9:
            api_path_appended << query.asset_name.substr(0,3) << "/" << query.asset_name.substr(3,3) << "?apiKey=" << query.api_key;
        // Real Time Currency Conversion
        case 10:
            api_path_appended << query.asset_name.substr(0,3) << "/" << query.asset_name.substr(3,3) << "?amount=" <<
            query.amount_currency << "&apiKey=" << query.api_key;
        //  Snapshots Option Contract
        case 11:
            api_path_appended << query.asset_ticker << "/" << query.asset_name << "?apiKey=" << query.api_key;
        // Snapshots Option Chain
        case 12:
            api_path_appended << query.asset_ticker << "?limit=" << query.limit << "&apiKey=" << query.api_key;
        // Snapshots Universal Snapshots
        case 13:
            // Implement Logic to handle the portfolio class
        // Does not work for FX
        case 14:
            api_path_appended << APIStringGenerator::string_locale_append(query) << "tickers?"<< query.asset_name
            << "&apiKey=" << query.api_key;
        // Ticker Events
        case 15:
            api_path_appended << APIStringGenerator::string_locale_append(query) << query.gainers_losers << "?apiKey=" <<
            query.api_key; 
        case 16:
            api_path_appended << APIStringGenerator::string_locale_append(query) << "tickers/"
            << APIStringGenerator::string_asset_append(query) <<"?apiKey=" <<
            query.api_key; 
        case 17:
            api_path_appended << APIStringGenerator::string_asset_append(query) << query.asset_name << query.limit << "?apiKey=" <<
            query.api_key; 
        // Still Need to develop support for the other 17 API requests
        default:
            api_path_appended << APIStringGenerator::string_asset_append(query) << "range/" << query.time_multiplier << "/" << 
            query.time_span << "/" << query.start_date << "/" << query.end_date << "?adjusted" << query.adjusted << "&sort="
            << query.sort << "&apiKey=" << query.api_key;;
    }    
    return api_path_appended.str();

}

template<typename T, typename... Types>
struct is_any : std::disjunction<std::is_same<T, Types>...> {};

std::string APIStringGenerator::string_asset_append(const QueryData& query){
    std::ostringstream stream;
    if (query.asset_type == QueryAsset::OPTIONS ||
        query.asset_type == QueryAsset::FOREX ||
        query.asset_type == QueryAsset::CRYPTO ||
        query.asset_type == QueryAsset::INDICES) {
        stream << static_cast<char>(query.asset_type) << ":" << query.asset_name << "/";
    } else {
        stream << query.asset_name << "/";
    }
    return stream.str();
};

std::string APIStringGenerator::string_locale_append(const QueryData& query){
    std::ostringstream stream;
    if (query.asset_type == QueryAsset::FOREX) {
        stream << "global/market/fx/";
    } else if (query.asset_type == QueryAsset::CRYPTO){
        stream << "global/market/crypto/";
    } else if (query.asset_type == QueryAsset::STOCKS){
        stream << "us/market/stocks/";
    }
    return stream.str();
};
