import SwiftUI

struct DashboardView: View {
    @State private var xpProgress: CGFloat = 0.4
    @State private var userLevel: Int = 1
    @State private var marketData: [MarketItem] = [
        MarketItem(name: "AAPL", price: 175.3, change: 1.5),
        MarketItem(name: "BTC", price: 52000.4, change: -0.8)
    ]
    
    var body: some View {
        ScrollView {
            VStack(spacing: 20) {
                // XP Progress Bar
                VStack {
                    Text("🔥 Trading XP")
                        .font(.title2)
                        .bold()
                    ProgressView(value: xpProgress)
                        .progressViewStyle(LinearProgressViewStyle(tint: .green))
                        .frame(width: 250, height: 10)
                        .background(Color.gray.opacity(0.3))
                        .cornerRadius(5)
                    Text("Level \(userLevel)")
                        .font(.subheadline)
                }
                .padding()
                .background(Color.black.opacity(0.3))
                .cornerRadius(12)

                // Market Prices
                VStack {
                    Text("📈 Live Market Prices")
                        .font(.title2)
                        .bold()
                    ForEach(marketData, id: \.id) { item in
                        MarketRow(item: item)
                    }
                }
                .padding()
                .background(Color.black.opacity(0.3))
                .cornerRadius(12)
            }
            .padding()
        }
        .background(Color.black.ignoresSafeArea())
        .foregroundColor(.white)
    }
}

struct MarketRow: View {
    var item: MarketItem
    var body: some View {
        HStack {
            Text(item.name)
                .font(.headline)
            Spacer()
            Text("$\(String(format: "%.2f", item.price))")
                .foregroundColor(item.change >= 0 ? .green : .red)
        }
        .padding()
        .background(Color.black.opacity(0.2))
        .cornerRadius(8)
    }
}
