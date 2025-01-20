
#include "TimeSeries.h"
#include <cmath>
#include <stdexcept>

TimeSeries::TimeSeries(const std::string& assetName, const std::string& startDate, const std::string& endDate)
    : assetName(assetName), startDate(startDate), endDate(endDate) {}

void TimeSeries::fetchTimeSeries(APIConnection& apiConnection) {
    auto data = apiConnection.sendRequest("/time_series_data");
    for (const auto& item : data["prices"]) {
        priceData.push_back(item);
    }
    for (const auto& item : data["timestamps"]) {
        timestamps.push_back(item);
    }
}

double TimeSeries::calculateVolatility() const {
    if (priceData.size() < 2) throw std::runtime_error("Insufficient data for volatility calculation");
    
    double mean = std::accumulate(priceData.begin(), priceData.end(), 0.0) / priceData.size();
    double variance = 0.0;
    for (double price : priceData) {
        variance += (price - mean) * (price - mean);
    }
    return std::sqrt(variance / (priceData.size() - 1));
}

nlohmann::json TimeSeries::toJSON() const {
    nlohmann::json j;
    j["assetName"] = assetName;
    j["startDate"] = startDate;
    j["endDate"] = endDate;
    j["priceData"] = priceData;
    j["timestamps"] = timestamps;
    return j;
}
