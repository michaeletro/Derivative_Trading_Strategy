#include "../../headers/AssetClasses/StockClass.h"

// ✅ Print Stock Data
void Stock::print() const {
    std::cout << "📈 Stock: ";
    Asset::print();
}
