#include "../../headers/APIConnection/APIStringGeneratorClass.h"
#include <sstream>
#include <concepts>
#include <iostream>
#include <type_traits>


// ✅ Generate complete API URL based on `QueryData`
std::string APIStringGenerator::generateURL(const QueryData& query) {
    std::ostringstream url_stream;

    // ✅ Start building the base URL
    url_stream << query.base_url;

    // ✅ Get API endpoint for the specified query type
    if (query.rest_api_query_type != RestAPIQueryType::NONE) {
        url_stream << APIStringGenerator::RestAPIQueryTypeGenerate(query);
    } else {
        throw std::invalid_argument("❌ Error: Invalid RestAPIQueryType!");
    }

    // ✅ Append required query parameters dynamically
    url_stream << "?adjusted=" << (query.adjusted ? "true" : "false");
    url_stream << "&sort=" << query.sort;
    url_stream << "&limit=" << query.limit;
    url_stream << "&apiKey=" << query.api_key;

    if (query.debug) {
        std::cout << "🔍 Generated URL: " << url_stream.str() << std::endl;
    }

    return url_stream.str();
}

// ✅ Retrieve API endpoint string from `endpointMap`
std::string APIStringGenerator::RestAPIQueryTypeGenerate(const QueryData& query) {
    // String to work on
    std::string api_path;

    // ✅ Validate query type
    if (firstEndPointMap.find(query.rest_api_query_type) == firstEndPointMap.end()) {
        throw std::invalid_argument("❌ Error: Unknown RestAPIQueryType!");
    }

    api_path = firstEndPointMap.at(query.query_type)
    api_path += APIStringGenerator::SecondPartGeneration(query.asset_type)

    if (debug) {
        std::cout << api_path << std::endl;
    }

    return firstEndPointMap.at(query.query_type);
}

template<typename T, typename... Types>
struct is_any : std::disjunction<std::is_same<T, Types>...> {};


template<typename T1, typename T2>
std::string APIStringGenerator::SecondPartGeneration(T1 QueryAssetType, T1 AssetType){
    std::ostringstream stream;

    if constexpr(std::is_same<T1, RestAPIQueryType::GROUPEDDAILY> &&
         is_any<T2, QueryAsset::FOREX, QueryAsset::STOCKS, QueryAsset::CRYPTO>) {
        if constexpr(std::is_same<T2, QueryAsset::CRYPTO>){
            stream << "global/market/fx/";
        } else if constexpr(std::is_same<T2, QueryAsset::FOREX>){
            stream << "global/market/crypto/";
        } else {
            stream << "us/market/stocks/";
        }
    } else {
        return "";
    }
}

template<typename T>
std::string APIStringGenerator::ThirdPartGeneration(T QueryAssetType){
    if constexpr (is_any<T, QueryAsset::OPTION, QueryAsset::FOREX,
         QueryAsset::CRYPTO, QueryAsset::INDICES>::value) {
        ostringstream stream << static_cast<char>QueryAssetType << ":";
        return stream.str();
    } else {
        return "";
    }
}

template<typename T>
std::string APIStringGenerator::FourthPartGeneration(T QueryAPIRequestType){
    if constexpr (is_any<T, RestAPIQueryType::OPTION, QueryAsset::FOREX,
         QueryAsset::CRYPTO, QueryAsset::INDICES>::value) {
        ostringstream stream << static_cast<char>QueryAssetType << ":";
        return stream.str();
    } else {
        return "";
    }
}