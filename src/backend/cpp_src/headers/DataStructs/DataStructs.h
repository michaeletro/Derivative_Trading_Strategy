#ifndef DATASTRUCTS_H
#define DATASTRUCTS_H

#include <string>
#include <array>
#include <string_view>
#include <cstdint>

//Struct to store query parameters for API Request

// Struct to store asset data
struct AssetData {
    std::string ticker;
    float open_price;
    float close_price;
    float high_price;
    float low_price;
    uint64_t volume;
};
struct StockData {
    std::string stock_id; // Unique identifier for the stock
    AssetData asset_data;
    std::string stock_name; // Name of the stock
    std::string exchange; // Exchange where the stock is listed
    std::string sector; // Sector of the stock
};
struct BondData {
    std::string bond_id; // Unique identifier for the bond
    AssetData asset_data;
    std::string bond_name; // Name of the bond
    std::string issuer; // Issuer of the bond
    float coupon_rate; // Coupon rate of the bond
    std::string maturity_date; // Maturity date of the bond
};
struct ETFData {
    std::string etf_id; // Unique identifier for the ETF
    AssetData asset_data;
    std::string etf_name; // Name of the ETF
    std::string underlying_index; // Underlying index of the ETF
    float expense_ratio; // Expense ratio of the ETF
};
struct MutualFundData {
    std::string mutual_fund_id; // Unique identifier for the mutual fund
    AssetData asset_data;
    std::string mutual_fund_name; // Name of the mutual fund
    std::string fund_family; // Fund family of the mutual fund
    float expense_ratio; // Expense ratio of the mutual fund
};
struct CommodityData {
    std::string commodity_id; // Unique
    AssetData asset_data;
    std::string commodity_name; // Name of the commodity
    std::string commodity_type; // Type of the commodity (e.g., gold, oil)
    float spot_price; // Current spot price of the commodity
    std::string unit; // Unit of measurement (e.g., ounces, barrels)
};


struct ForexData {
    std::string forex_id; // Unique identifier for the forex pair
    AssetData asset_data;
    std::string forex_pair; // e.g., "EUR/USD"
    float exchange_rate; // Current exchange rate
};
struct CryptoData {
    std::string crypto_id; // Unique identifier for the cryptocurrency
    AssetData asset_data;
    std::string crypto_symbol; // Symbol of the cryptocurrency
    float market_cap; // Market capitalization
    float circulating_supply; // Circulating supply of the cryptocurrency
};
struct IndexData {
    std::string index_id; // Unique identifier for the index
    AssetData asset_data;
    std::string index_name; // Name of the index
    float index_value; // Current value of the index
};
struct OptionContractData {
    std::string option_contract_id; // Unique identifier for the option contract
    AssetData asset_data;
    std::string option_symbol; // Symbol of the option contract
    std::string option_type; // "call" or "put"
    std::string expiry_date;
    float strike_price;
    float implied_volatility;
    bool is_call;
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
    for (long unsigned int i = 0; i < firstEndPointMap.size(); i++){
        if (firstEndPointMap[i].type == query_type){
            return i;
        }
    }
    return -1;
}

#endif //DATASTRUCTS_