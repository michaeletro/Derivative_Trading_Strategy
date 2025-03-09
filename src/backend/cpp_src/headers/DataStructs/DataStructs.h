#ifndef DATASTRUCTS_H
#define DATASTRUCTS_H

#include <string>
#include <array>
#include <string_view>
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
    GROUPEDDAILY,
    IPOS,
    LASTTRADE,
    LASTTRADECRYPTOPAIR,
    LASTQUOTE,
    LASTQUOTECURRENCYPAIR,
    REALTIMECURRENCYCONVERSION,
    MARKETHOLIDAYS,
    MARKETSTATUS,
    OPTIONSCONTRACTS,
    OPTIONCONTRACT,
    PREVIOUSCLOSE,
    QUOTES,
    RELATEDCOMPANIES,
    SNAPSHOTSALLTICKERS,
    SNAPSHOTSGAINERSLOSERS,
    SNAPSHOTSTICKER,
    SNAPSHOTSUNIVERSALSNAPSHOT,
    SNAPSHOTSOPTIONCONTRACT,
    SNAPSHOTSOPTIONCHAIN,
    STOCKSPLITSV3,
    STOCKFINANCIALSVX,
    TECHNICALINDICATORS,
    TICKERS,
    TICKERDETAILSV3,
    TICKEREVENTS,
    TICKERNEWS,
    TICKERTYPES,
    TRADES
};

enum class WebSocketQueryType {
    NONE,
    AGGREGATESMINUTES,
    AGGREGATESSSECONDS,
    FAIRMARKETVALUE,
    QUOTES,
    TRADES
};

struct APIEndpointMapping {
    RestAPIQueryType type;
    std::string_view endpoint;
};


struct QueryData {
    // Asset Type and Types of Query To Make
    QueryAsset asset_type = QueryAsset::NONE;
    RestAPIQueryType rest_api_query_type = RestAPIQueryType::NONE;
    WebSocketQueryType web_socket_query_type = WebSocketQueryType::NONE;

    // Standard Query Parameters with predefined types
    std::string base_url = "https://api.polygon.io";
    std::string start_date = "2024-01-09";
    std::string end_date = "2025-01-09";
    std::string time_span = "day";
    std::string sort = "asc";

    // Specific Queryy Parameters
    std::string asset_name;
    std::string ticker_name;
    std::string date_of_interest;
    std::string api_key;
    
    // Size of Query and the number of elements to request
    short limit = 5000;
    short time_multiplier = 1;
    std::string adjusted = "true";
    bool debug = true;
    float amount_currency = 100.00;
    std::string asset_ticker;
    std::string gainers_losers = "gainers";
};

constexpr std::array<APIEndpointMapping,34> firstEndPointMap  = {{
    {RestAPIQueryType::AGGREGATES, "/v2/aggs/ticker/"},
    {RestAPIQueryType::GROUPEDDAILY, "/v2/aggs/grouped/locale/"},
    {RestAPIQueryType::DAILYOPENCLOSE, "/v1/open-close/"},
    {RestAPIQueryType::PREVIOUSCLOSE, "/v2/aggs/ticker/"},
    {RestAPIQueryType::TRADES, "/v3/trades/"},
    {RestAPIQueryType::LASTTRADE, "/v2/last/trade/"},
    {RestAPIQueryType::LASTTRADECRYPTOPAIR, "/v1/last/crypto/"},
    {RestAPIQueryType::QUOTES, "/v3/quotes/"},
    {RestAPIQueryType::LASTQUOTE, "/v2/last/nbbo/"},
    {RestAPIQueryType::LASTQUOTECURRENCYPAIR, "/v1/last_quote/currencies/"},
    {RestAPIQueryType::REALTIMECURRENCYCONVERSION, "/v1/conversion/"},
    {RestAPIQueryType::SNAPSHOTSOPTIONCONTRACT, "/v3/snapshot/options/"}, // 11
    {RestAPIQueryType::SNAPSHOTSOPTIONCHAIN, "/v3/snapshot/options/"},
    {RestAPIQueryType::SNAPSHOTSUNIVERSALSNAPSHOT, "/v3/snapshot?ticker.any_of="}, // 13
    {RestAPIQueryType::SNAPSHOTSALLTICKERS, "/v2/snapshot/locale/"},
    {RestAPIQueryType::SNAPSHOTSGAINERSLOSERS, "/v2/snapshot/locale/"},
    {RestAPIQueryType::SNAPSHOTSTICKER, "/v2/snapshot/locale/"}, // 16

    {RestAPIQueryType::SNAPSHOTSUNIVERSALSNAPSHOT, "/v3/snapshot?ticker.any_of="}, // 17
    {RestAPIQueryType::TICKERS, "/v3/reference/tickers?active=true&"},
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
    {RestAPIQueryType::OPTIONCONTRACT, "/v3/reference/options/contracts/"},
    {RestAPIQueryType::OPTIONSCONTRACTS, "/v3/reference/options/contracts?limit="}
}};


constexpr std::string_view getEndPoint(RestAPIQueryType query_type) {
    for (const auto& mapping : firstEndPointMap) {
        if (mapping.type == query_type){
            return mapping.endpoint;
        }
    }
    return "UNKNOWN";
};

constexpr int getEndPointIndex(RestAPIQueryType query_type){
    for (short i = 0; i < firstEndPointMap.size(); i++){
        if (firstEndPointMap[i].type == query_type){
            return i;
        }
    }
    return -1;
}

#endif //DATASTRUCTS_