import SwiftUI

struct MainTabView: View {
    var body: some View {
        TabView {
            DashboardView()
                .tabItem {
                    Image(systemName: "house.fill")
                    Text("Dashboard")
                }
            LeaderboardView()
                .tabItem {
                    Image(systemName: "house.fill")
                    Text("LeaderBoard")
                }
            MarketView()
                .tabItem {
                    Image(systemName: "house.fill")
                    Text("MarketView")
                }
            PortfolioView()
                .tabItem {
                    Image(systemName: "house.fill")
                    Text("PortfolioView")
                }
            SettingsView()
                .tabItem {
                    Image(systemName: "house.fill")
                    Text("SettingsView")
                }
            
            MarketDataView()
                .tabItem {
                    Image(systemName: "chart.bar.fill")
                    Text("Market")
                }
            
            GamificationView()
                .tabItem {
                    Image(systemName: "star.fill")
                    Text("XP & Achievements")
                }
        }
        .accentColor(.green)
    }
}
