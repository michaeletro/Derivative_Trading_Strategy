import SwiftUI

struct SettingsView: View {
    @State private var darkMode = false

    var body: some View {
        Form {
            Toggle("Dark Mode", isOn: $darkMode)
            Button("Reset Data") {
                print("Data Reset")
            }
        }
        .navigationTitle("⚙️ Settings")
    }
}
