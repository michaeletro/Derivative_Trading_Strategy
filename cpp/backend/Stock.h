
#ifndef STOCK_H
#define STOCK_H

#include "Asset.h"
#include <unordered_map>
#include <vector>

class Stock : public Asset {
private:
    std::unordered_map<std::string, std::vector<double>> optionData; // Call/put data

public:
    Stock(const std::string& stockName);
    void fetchOptionData(APIConnection& apiConnection);
    void calculateVolatilitySurface();
};

#endif
