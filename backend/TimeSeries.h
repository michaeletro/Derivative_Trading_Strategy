
#ifndef TIME_SERIES_H
#define TIME_SERIES_H

#include <string>
#include <vector>
#include "../libs/json/single_include/nlohmann/json.hpp"
#include "APIConnection.h"

class TimeSeries {
private:
    std::string assetName;
    std::string startDate;
    std::string endDate;
    std::vector<double> priceData;
    std::vector<std::string> timestamps;

public:
    TimeSeries(const std::string& assetName, const std::string& startDate, const std::string& endDate);
    void fetchTimeSeries(APIConnection& apiConnection);
    double calculateVolatility() const;
    nlohmann::json toJSON() const;
};

#endif
