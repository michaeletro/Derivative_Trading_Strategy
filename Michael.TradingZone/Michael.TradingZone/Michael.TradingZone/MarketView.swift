import SwiftUI

struct MarketView: View {
    let stocks = [
        ("AAPL", "$150.32", "+2.5%"),
        ("TSLA", "$720.45", "-1.3%"),
        ("BTC", "$42,000", "+5.1%")
    ]

    var body: some View {
        List(stocks, id: \.0) { stock in
            HStack {
                Text(stock.0)
                    .font(.headline)
                    .foregroundColor(.white)

                Spacer()

                Text(stock.1)
                    .fontWeight(.bold)
                    .foregroundColor(.green)

                Text(stock.2)
                    .foregroundColor(stock.2.contains("-") ? .red : .green)
            }
            .padding()
        }
        .background(Color.black)
        .navigationTitle("📊 Market")
    }
}
