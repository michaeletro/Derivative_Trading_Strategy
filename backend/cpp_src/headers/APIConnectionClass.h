#ifndef API_CONNECTION_H
#define API_CONNECTION_H

#include <string>
#include <map>
#include <memory>
#include <stdexcept>
#include <rapidjson/document.h>
#include <vector>

#include "APIStringGeneratorClass.h"


struct APIResult {
    double volume;
    double volume_weighted_price;
    double open_price;
    double close_price;
    double highest_price;
    double lowest_price;
    long long timestamp;
    int num_transactions;
};

class APIConnection {
protected:
    // Attributes
    std::string asset_name;
    std::string start_date;
    std::string end_date;
    std::string time_multiplier;
    std::string time_span;
    std::string sort;
    std::string api_key;

    int limit;

    bool adjusted;
    bool debug = true;

    // Build URL dynamically
    std::string buildURL() const;

    // Validation methods
    virtual void validateParameters() const;
    virtual void validateResponse(const rapidjson::Document& response) const;

public:
    // Constructors and Destructor
    APIConnection(const std::string& asset_name, const std::string& start_date = "2023-01-09",
                  const std::string& end_date = "", const std::string& time_multiplier = "1",
                  const std::string& time_span = "day", const std::string& sort = "asc",
                  const std::string& api_key = "YOUR_API_KEY", int limit = 5000,
                  bool adjusted = true, bool debug = true);
    virtual ~APIConnection() = default;

    // Deleted functions to prevent copying
    APIConnection(const APIConnection&) = delete;
    APIConnection& operator=(const APIConnection&) = delete;

    // Move semantics
    APIConnection(APIConnection&&) noexcept = default;
    APIConnection& operator=(APIConnection&&) noexcept = default;

    // API Interaction
    std::vector<APIResult> fetchAPIData() const;
};

#endif // API_CONNECTION_H
