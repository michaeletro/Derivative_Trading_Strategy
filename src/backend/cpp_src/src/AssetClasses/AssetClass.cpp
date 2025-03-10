#include "../../headers/AssetClasses/AssetClass.h"

// ✅ Print Asset Data
void Asset::print() const {
    std::cout << "📊 " << ticker << " | " << date
              << " | Open: " << open_price << " | Close: " << close_price
              << " | High: " << high_price << " | Low: " << low_price
              << " | Volume: " << volume << "\n";
}
