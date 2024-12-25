#include "Asset.h"
#include <numeric>
#include <stdexcept>

Asset::Asset(const std::string& assetName) : name(assetName) {}

void Asset::fetchPriceData(APIConnection& apiConnection) {
    auto data = apiConnection.sendRequest("/asset_price_data");
    for (const auto& item : data["prices"]) {
        priceData.push_back(item);
    }
}

double Asset::calculateMovingAverage(size_t windowSize) const {
    if (priceData.size() < windowSize) throw std::runtime_error("Insufficient data for moving average");
    double sum = std::accumulate(priceData.end() - windowSize, priceData.end(), 0.0);
    return sum / windowSize;
}

nlohmann::json Asset::toJSON() const {
    nlohmann::json j;
    j["name"] = name;
    j["priceData"] = priceData;
    return j;
}