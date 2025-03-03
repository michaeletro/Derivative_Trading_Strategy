#ifndef DATASTRUCTS_H
#define DATASTRUCTS_H

#include <string>

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
    GROUPEDAILY;
    IPOS,
    LASTTRADE,
    LASTQUOTE,
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
// ✅ Define `endpointMap` outside any function (Global Static)
const std::unordered_map<RestAPIQueryType, std::string> endpointMap = {
    {RestAPIQueryType::AGGREGATES, "/v2/aggs/ticker/"},
    {RestAPIQueryType::GROUPEDDAILY, "/v2/aggs/grouped/locale/"},
    {RestAPIQueryType::DAILYOPENCLOSE, "/v1/open-close/"},
    {RestAPIQueryType::PREVIOUSCLOSE, "/v2/aggs/ticker/"},
    {RestAPIQueryType::TRADES, "/v3/trades/"},
    {RestAPIQueryType::LASTTRADE, "/v2/last/trade/"},
    {RestAPIQueryType::QUOTES, "/v3/quotes/"},
    {RestAPIQueryType::LASTQUOTE, "/v2/last/"},
    {RestAPIQueryType::SNAPSHOTSALLTICKERS, "/v2/snapshot/locale/"},
    {RestAPIQueryType::SNAPSHOTSGAINERSLOSERS, "/v2/snapshot/locale/"},
    {RestAPIQueryType::SNAPSHOTSTICKER, "/v2/snapshot/locale/"},
    {RestAPIQueryType::SNAPSHOTSUNIVERSALSNAPSHOT, "/v3/snapshot?ticker.any_of="},
    {RestAPIQueryType::TICKERS, "/v3/reference/tickers?active="},
    {RestAPIQueryType::TICKERDETAILSV3, "/v3/reference/tickers/"},
    {RestAPIQueryType::TICKEREVENTS, "/vx/reference/tickers/"},
    {RestAPIQueryType::TICKERNEWS, "/v2/reference/news?limit="},
    {RestAPIQueryType::TICKERTYPES, "/v3/reference/tickers/types?asset_class="},
    {RestAPIQueryType::MARKETHOLIDAYS, "/v1/marketstatus/upcoming?apikey="},
    {RestAPIQueryType::MARKETSTATUS, "/v1/marketstatus/now?apikey="},
    {RestAPIQueryType::STOCKSPLITSV3, "/v3/reference/splits?limit="},
    {RestAPIQueryType::DIVIDENDSV3, "/v3/reference/dividends?limit="},
    {RestAPIQueryType::STOCKFINANCIALSVX, "/vX/reference/financials?limit="},
    {RestAPIQueryType::CONDITIONS, "/v3/reference/conditions?asset_class="},
    {RestAPIQueryType::EXCHANGES, "/v3/reference/exchanges?asset_class="},
    {RestAPIQueryType::RELATEDCOMPANIES, "/v1/related-companies/"},
    {RestAPIQueryType::IPOS, "/vX/reference/ipos?limit="},
};

// Declare EndPoint Map as extern
extern const std::unordered_map<RestAPIQueryType, std::string> endpointMap;

enum class WebSocketQueryType {
    NONE,
    AGGREGATESMINUTES,
    AGGREGATESSSECONDS,
    FAIRMARKETVALUE,
    QUOTES,
    TRADES
}

struct QueryData {
    // Asset Type and Types of Query To Make
    QueryAsset asset_type = QueryAsset::NONE;
    RestAPIQueryType rest_api_query_type = RestAPIQueryType::None;
    WebSocketQueryType qeb_socket_query_tye = WebSocketQueryType::None;

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