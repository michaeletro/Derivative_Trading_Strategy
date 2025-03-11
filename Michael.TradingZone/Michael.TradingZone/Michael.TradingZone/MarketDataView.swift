import SwiftUI

struct MarketDataView: View {
    @State private var marketData: [MarketItem] = []
    
    var body: some View {
        VStack {
            List(marketData, id: \.id) { item in
                MarketRow(item: item)
            }
            .onAppear {
                fetchMarketData()
            }
        }
        .background(Color.black.ignoresSafeArea())
    }
    
    func fetchMarketData() {
        guard let url = URL(string: "http://your-api.com/market") else { return }
        URLSession.shared.dataTask(with: url) { data, response, error in
            if let data = data {
                if let decoded = try? JSONDecoder().decode([MarketItem].self, from: data) {
                    DispatchQueue.main.async {
                        self.marketData = decoded
                    }
                }
            }
        }.resume()
    }
}
