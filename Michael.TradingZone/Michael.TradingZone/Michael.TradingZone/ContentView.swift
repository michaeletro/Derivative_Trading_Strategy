import SwiftUI

struct ContentView: View {
    var body: some View {
        TabView {
            DashboardView()
                .tabItem {
                    Image(systemName: "house.fill")
                    Text("Dashboard")
                }

            MarketView()
                .tabItem {
                    Image(systemName: "chart.bar.fill")
                    Text("Market")
                }

            PortfolioView()
                .tabItem {
                    Image(systemName: "briefcase.fill")
                    Text("Portfolio")
                }

            LeaderboardView()
                .tabItem {
                    Image(systemName: "trophy.fill")
                    Text("Leaderboard")
                }

            SettingsView()
                .tabItem {
                    Image(systemName: "gearshape.fill")
                    Text("Settings")
                }
        }
        .accentColor(.green)
    }
}
