#include "../../headers/Assets/Stock.h"

// ✅ Print Stock Data
void Stock::print() const {
    std::cout << "📈 Stock: ";
    Asset::print();
}
