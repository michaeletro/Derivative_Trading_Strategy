#include "../../headers/AssetClasses/TimeSeriesClass.h"

// ✅ Load Data from Database
template <typename T>
void TimeSeries<T>::loadFromDatabase(DataBaseClass* db, const std::string& ticker, 
                                     const std::string& startDate, const std::string& endDate, 
                                     int limit, bool ascending) {
    if (!db) {
        std::cerr << "❌ Error: Database connection is null!\n";
        return;
    }

    auto results = db->queryAssetData(ticker, startDate, endDate, limit, ascending);

    // ✅ Fix: Ensure valid asset type before inserting into TimeSeries
    if constexpr (std::is_same_v<T, std::shared_ptr<Stock>> ||
                  std::is_same_v<T, std::shared_ptr<Option>> ||
                  std::is_same_v<T, std::shared_ptr<Crypto>> ||
                  std::is_same_v<T, std::shared_ptr<Forex>>) {
        for (const auto& row : results) {
            data.push_back(std::make_shared<typename T::element_type>(
                row.ticker//, row.date, row.open_price, row.close_price, 
                //row.high_price, row.low_price, row.volume
            ));
        }
    } else {
        static_assert(dependent_false<T>::value, "❌ Unsupported asset type in TimeSeries.");
    }
}

// ✅ Calculate Average Closing Price
template <typename T>
double TimeSeries<T>::calculateAverageClosingPrice() const {
    if (data.empty()) return 0.0;
    return std::accumulate(data.begin(), data.end(), 0.0, 
        [](double sum, const T& asset) { return sum + asset->getClosePrice(); }) / data.size();
}

// ✅ Find Maximum Closing Price
template <typename T>
double TimeSeries<T>::findMaxClose() const {
    if (data.empty()) return 0.0;
    return std::max_element(data.begin(), data.end(), 
        [](const T& a, const T& b) { return a->getClosePrice() < b->getClosePrice(); })->get()->getClosePrice();
}

// ✅ Find Minimum Closing Price
template <typename T>
double TimeSeries<T>::findMinClose() const {
    if (data.empty()) return 0.0;
    return std::min_element(data.begin(), data.end(), 
        [](const T& a, const T& b) { return a->getClosePrice() < b->getClosePrice(); })->get()->getClosePrice();
}

// ✅ Calculate Volatility
template <typename T>
double TimeSeries<T>::calculateVolatility() const {
    if (data.size() < 2) return 0.0;
    double avg = calculateAverageClosingPrice();
    double variance = std::accumulate(data.begin(), data.end(), 0.0,
        [avg](double sum, const T& asset) {
            double diff = asset->getClosePrice() - avg;
            return sum + diff * diff;
        }) / data.size();

    return std::sqrt(variance);
}

// ✅ Calculate Moving Average
template <typename T>
double TimeSeries<T>::calculateMovingAverage(int period) const {
    if (static_cast<size_t>(period) > data.size()) return 0.0;
    double sum = 0.0;
    for (size_t i = data.size() - period; i < data.size(); i++) {
        sum += data[i]->getClosePrice();
    }
    return sum / period;
}

// ✅ Print Time Series Data
template <typename T>
void TimeSeries<T>::printTimeSeries() const {
    std::cout << "\n📊 Time Series Data (" << data.size() << " records):\n";
    for (const auto& asset : data) {
        asset->print();
    }
}

// ✅ Explicit Instantiations
template class TimeSeries<std::shared_ptr<Stock>>;
template class TimeSeries<std::shared_ptr<Option>>;
template class TimeSeries<std::shared_ptr<Crypto>>;
template class TimeSeries<std::shared_ptr<Forex>>;
