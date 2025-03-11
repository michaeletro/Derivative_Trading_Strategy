import SwiftUI

struct LeaderboardView: View {
    let traders = [
        ("Mike Quant", "$1.2M"),
        ("Jane Doe", "$900K"),
        ("Elon Trader", "$750K")
    ]

    var body: some View {
        List(traders, id: \.0) { trader in
            HStack {
                Text(trader.0)
                    .font(.headline)
                Spacer()
                Text(trader.1)
                    .fontWeight(.bold)
                    .foregroundColor(.green)
            }
            .padding()
        }
        .navigationTitle("🏆 Leaderboard")
    }
}

