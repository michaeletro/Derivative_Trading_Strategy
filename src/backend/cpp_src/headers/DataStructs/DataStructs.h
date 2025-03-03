#ifndef DATASTRUCTS_H
#define DATASTRUCTS_H

#include <string>
#include <cstdint>

//Struct to store query parameters for API Request

// Struct to store asset data
struct AssetData {
    int id;
    std::string ticker;
    std::string date;
    float open_price;
    float close_price;
    float high_price;
    float low_price;
    uint64_t volume;
};

// Enum to specify different query types
enum class QueryAsset {
    NONE,
    STOCKS,
    FOREX = 'C',
    CRYPTO = 'X',
    OPTIONS = 'O',
    INDICES = 'I'
};

enum class RestAPIQueryType {
    NONE,
    AGGREGATES,
    CONDITIONS,
    DAILYOPENCLOSE,
    DIVIDENDSV3,
    EXCHANGES,
    GROUPEDAILY,
    IPOS,
    LASTTRADE,
    LASTTRADECRYPTOPAIR,
    LASTQUOTE,
    LASTQUOTECURRENCYPAIR,
    REALTIMECURRENCYCONVERSION,
    MARKETHOLIDAYS,
    MARKETSTATUS,
    OPTIONSCONTRACT,
    OPTIONCONTRACTS,
    PREVIOUSCLOSE,
    QUOTES,
    RELATEDCOMPANIES,
    SNAPSHOTSALLTICKERS,
    SNAPSHOTSGAINERSLOSERS,
    SNAPSHOTSTICKER,
    SNAPSHOTSUNIVERSALSNAPSHOT,
    SNAPSHOTSOPTIONCONTRACT,
    SNAPSHOTSOPTIONCHAIN,
    SNAPSHOTSUNIVERSALSNAPSHOT,
    STOCKSPLITSV3,
    STOCKFINANCIALSVX,
    TECHNICALINDICATORS,
    TICKERS,
    TICKERDETAILSV3,
    TICKEREVENTS,
    TICKERNEWS,
    TICKERTYPES,
    TRADES
}

// Declare EndPoint Map as extern
extern const std::unordered_map<RestAPIQueryType, std::string> firstEndPointMap;

enum class WebSocketQueryType {
    NONE,
    AGGREGATESMINUTES,
    AGGREGATESSSECONDS,
    FAIRMARKETVALUE,
    QUOTES,
    TRADES
};

struct QueryData {
    // Asset Type and Types of Query To Make
    QueryAsset asset_type = QueryAsset::NONE;
    RestAPIQueryType rest_api_query_type = RestAPIQueryType::NONE;
    WebSocketQueryType qeb_socket_query_tye = WebSocketQueryType::NONE;

    // Standard Query Parameters with predefined types
    std::string base_url = "https://api.polygon.io/";
    std::string start_date = "2024-01-09";
    std::string end_date = "2025-01-09";
    std::string time_span = "day";
    std::string sort = "asc";

    // Specific Queryy Parameters
    std::string asset_name;
    std::string api_key;
    
    // Size of Query and the number of elements to request
    short limit = 5000;
    short time_multiplier = 1;
    bool adjusted = true;
    bool debug = true;
};

#endif //DATASTRUCTS_