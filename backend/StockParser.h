#ifndef STOCKPARSER_H
#define STOCKPARSER_H

#include <vector>
#include <string>
#include "../libs/rapidjson/include/rapidjson/document.h"

// Structure to hold individual stock data
struct StockData {
    double volume;
    double vw_price;
    double open_price;
    double close_price;
    double high_price;
    double low_price;
    long long timestamp;
    int trades;
};

// Class to parse and manage stock data from JSON responses
class StockParser {
public:
    std::string ticker;
    bool adjusted;
    std::string status;
    std::vector<StockData> results;

    // Method to parse JSON from a string
    void parseFromJSON(const std::string& jsonString);

    // Print the parsed data
    void print() const;
};

#endif // STOCKPARSER_H
