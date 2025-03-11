import SwiftUI

struct GamificationView: View {
    @State private var achievements = [
        "First Trade", "100 Trades", "Market Guru"
    ]
    
    var body: some View {
        VStack {
            Text("🏆 Achievements")
                .font(.title)
                .bold()
            
            List(achievements, id: \.self) { achievement in
                Text("✅ \(achievement)")
            }
        }
        .padding()
        .background(Color.black.opacity(0.3))
        .cornerRadius(12)
        .foregroundColor(.white)
    }
}
