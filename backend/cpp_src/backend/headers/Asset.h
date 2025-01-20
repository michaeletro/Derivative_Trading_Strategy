
#ifndef ASSET_H
#define ASSET_H

#include <string>
#include <vector>
#include "../libs/json/single_include/nlohmann/json.hpp"
#include "APIConnection.h"

class Asset {
public:
    Asset(const std::string& assetName);
    void fetchPriceData(APIConnection& apiConnection);
    double calculateMovingAverage(size_t windowSize) const;
    nlohmann::json toJSON() const;

private:
    std::string name;
    std::vector<double> priceData;
};
#endif
