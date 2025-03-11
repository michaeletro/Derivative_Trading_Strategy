import Foundation

struct MarketItem: Identifiable, Codable {
    var id = UUID()
    let name: String
    let price: Double
    let change: Double
}
