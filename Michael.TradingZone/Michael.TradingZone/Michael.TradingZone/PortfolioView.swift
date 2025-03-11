import SwiftUI

struct PortfolioView: View {
    @State private var assets = [
        ("AAPL", "10 Shares", "$1500"),
        ("BTC", "0.5 BTC", "$21,000")
    ]

    var body: some View {
        List {
            ForEach(assets, id: \.0) { asset in
                HStack {
                    Text(asset.0)
                        .font(.headline)
                    Spacer()
                    Text(asset.1)
                    Text(asset.2)
                        .fontWeight(.bold)
                }
            }
            .onDelete { indexSet in
                assets.remove(atOffsets: indexSet)
            }
        }
        .navigationTitle("💼 Portfolio")
    }
}
