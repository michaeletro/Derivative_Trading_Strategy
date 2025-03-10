#ifndef TIMESERIES_H
#define TIMESERIES_H

#include <vector>
#include <iostream>
#include <numeric>
#include <algorithm>
#include <cmath>
#include "../DataBase/DataBaseClass.h"
#include "AssetClass.h"

template <typename T>
struct dependent_false : std::false_type {};

template <typename T>
class TimeSeries {
    static_assert(std::is_base_of_v<Asset, std::remove_pointer_t<typename T::element_type>>,
        "❌ TimeSeries<T> must be instantiated with an Asset-derived class (Stock, Option, Crypto, etc.).");
private:
    std::vector<T> data;

public:
    TimeSeries() = default;

    void addAsset(const T& asset) { data.push_back(asset); }
    void addAsset(T&& asset) { data.push_back(std::move(asset)); }

    void loadFromDatabase(DataBaseClass* db, const std::string& ticker, 
                          const std::string& startDate = "", const std::string& endDate = "", 
                          int limit = 1000, bool ascending = true);

    int size() const { return data.size(); }  // ✅ Added size() method

    T getAsset(int index) const {  // ✅ Added getAsset() method
        if (index >= 0 && index < static_cast<int>(data.size())) {
            return data[index];
        } else {
            throw std::out_of_range("Index out of range in TimeSeries");
        }
    }

    // ✅ Time Series Calculations
    double calculateAverageClosingPrice() const;
    double calculateVolatility() const;
    double findMaxClose() const;
    double findMinClose() const;
    double calculateMovingAverage(int period) const;

    void sortByDate(bool ascending = true);
    void printTimeSeries() const;

    // ✅ Getter for Time Series Data
    const std::vector<T>& getData() const { return data; }
};

// ✅ Now include Portfolio after defining TimeSeries
#include "PortfolioClass.h"

#endif // TIMESERIES_H
