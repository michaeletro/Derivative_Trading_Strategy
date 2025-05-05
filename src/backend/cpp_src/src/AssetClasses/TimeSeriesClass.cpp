#include "../../headers/AssetClasses/TimeSeriesClass.h"

// ✅ Load Data from Database
template <typename T>
void TimeSeries<T>::loadFromDatabase(DataBaseClass* db, const std::string& table_name, const std::string& ticker, 
    uint64_t startDate, uint64_t endDate, int limit, bool ascending) {

    if (!db) {
        std::cerr << "❌ Error: Database connection is null!\n";
        return;
    }

    auto results = db->queryData(table_name, ticker, formatDate(startDate), formatDate(endDate), limit, ascending);
    if (results.empty()) {
        std::cerr << "❌ No data found for ticker: " << ticker << "\n";
        return;
    }

    if constexpr (
        std::is_same_v<T, std::shared_ptr<Stock>> ||
        std::is_same_v<T, std::shared_ptr<Option>> ||
        std::is_same_v<T, std::shared_ptr<Crypto>> ||
        std::is_same_v<T, std::shared_ptr<Forex>>
    ) {
        for (const auto& row : results) {
            auto assetPtr = std::make_shared<typename T::element_type>();

            // Set shared fields
            assetPtr->id = row->id;
            assetPtr->ticker = row->ticker;
            assetPtr->date = row->date;
            assetPtr->open_price = row->open_price;
            assetPtr->close_price = row->close_price;
            assetPtr->high_price = row->high_price;
            assetPtr->low_price = row->low_price;
            assetPtr->volume = row->volume;

            // Downcast to specific type if needed
            if constexpr (std::is_same_v<T, std::shared_ptr<Stock>>) {
                const StockData* stockRow = static_cast<const StockData*>(row.get());
                assetPtr->stock_name = stockRow->stock_name;
                assetPtr->exchange = stockRow->exchange;
                assetPtr->sector = stockRow->sector;
            } else if constexpr (std::is_same_v<T, std::shared_ptr<Option>>) {
                const OptionData* optionRow = static_cast<const OptionData*>(row.get());
                assetPtr->option_symbol = optionRow->option_symbol;
                assetPtr->option_type = optionRow->option_type;
                assetPtr->expiry_date = optionRow->expiry_date;
                assetPtr->strike_price = optionRow->strike_price;
            }

            data.push_back(assetPtr);
        }
    } else {
        static_assert(dependent_false<T>::value, "❌ Unsupported asset type in TimeSeries.");
    }
}

template <typename T>
std::string TimeSeries<T>::formatDate(uint64_t date) const {
    std::time_t t = static_cast<std::time_t>(date);
    std::tm* tm = std::localtime(&t);
    char buffer[11]; // YYYY-MM-DD\0
    std::strftime(buffer, sizeof(buffer), "%Y-%m-%d", tm);
    return buffer;
}

// ✅ Calculate Average Closing Price
template <typename T>
double TimeSeries<T>::calculateAverageClosingPrice() const {
    if (data.empty()) return 0.0;

    double sum = 0.0;
    for (const auto& asset : data) {
        sum += asset->getClosePrice();
    }
    return sum / data.size();
}

// ✅ Find Maximum Closing Price
template <typename T>
double TimeSeries<T>::findMaxClose() const {
    if (data.empty()) return 0.0;

    auto maxIt = std::max_element(data.begin(), data.end(),
        [](const auto& a, const auto& b) {
            return a->getClosePrice() < b->getClosePrice();
        });

    return (*maxIt)->getClosePrice();
}

// ✅ Find Minimum Closing Price
template <typename T>
double TimeSeries<T>::findMinClose() const {
    if (getData().empty()) return 0.0;

    auto minIt = std::min_element(data.begin(), data.end(),
        [](const auto& a, const auto& b) {
            return a->getClosePrice() < b->getClosePrice();
        });

    return (*minIt)->getClosePrice();
}

// ✅ Calculate Volatility
template <typename T>
double TimeSeries<T>::calculateVolatility() const {
    if (getData().size() < 2) return 0.0;

    double avg = calculateAverageClosingPrice();
    double variance = 0.0;

    for (const auto& asset : data) {
        double diff = asset->getClosePrice() - avg;
        variance += diff * diff;
    }

    variance /= data.size();
    return std::sqrt(variance);
}

// ✅ Calculate Moving Average
template <typename T>
double TimeSeries<T>::calculateMovingAverage(int period) const {
    if (period <= 0 || static_cast<size_t>(period) > getData().size()) return 0.0;

    double sum = 0.0;
    for (size_t i = data.size() - period; i < getData().size(); ++i) {
        sum += data[i]->getClosePrice();
    }
    return sum / period;
}

// ✅ Print Time Series Data
template <typename T>
void TimeSeries<T>::printTimeSeries() const {
    std::cout << "\n📊 Time Series Data (" << getData().size() << " records):\n";
    for (const auto& asset : getData()) {
        asset->print();
    }
}
