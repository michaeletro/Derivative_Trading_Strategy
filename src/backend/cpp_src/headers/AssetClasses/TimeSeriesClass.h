#ifndef TIMESERIES_H
#define TIMESERIES_H

#include <vector>
#include <iostream>
#include <numeric>
#include <algorithm>
#include <cmath>
#include "../DataBase/DataBaseClass.h"
#include "../Assets/Asset.h"

template <typename T>
class TimeSeries {
private:
    std::vector<T> data;

public:
    TimeSeries() = default;

    void addAsset(const T& asset) { data.push_back(asset); }
    void addAsset(T&& asset) { data.push_back(std::move(asset)); }

    void loadFromDatabase(DataBaseClass* db, const std::string& ticker, 
                          const std::string& startDate = "", const std::string& endDate = "", 
                          int limit = 1000, bool ascending = true);

    // ✅ Time Series Calculations
    double calculateAverageClosingPrice() const;
    double calculateVolatility() const;
    double findMaxClose() const;
    double findMinClose() const;
    double calculateMovingAverage(int period) const;

    void sortByDate(bool ascending = true);
    void printTimeSeries() const;
};

#endif // TIMESERIES_H
