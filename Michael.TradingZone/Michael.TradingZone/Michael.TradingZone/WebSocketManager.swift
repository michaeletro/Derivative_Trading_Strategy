import Foundation

class WebSocketManager: ObservableObject {
    private var webSocketTask: URLSessionWebSocketTask?
    
    init() {
        connect()
    }
    
    func connect() {
        guard let url = URL(string: "ws://your-api.com/live_market") else { return }
        webSocketTask = URLSession.shared.webSocketTask(with: url)
        webSocketTask?.resume()
        receiveMessage()
    }
    
    private func receiveMessage() {
        webSocketTask?.receive { result in
            switch result {
            case .success(let message):
                switch message {
                case .string(let text):
                    print("Market Update: \(text)")
                default:
                    break
                }
            case .failure(let error):
                print("WebSocket Error: \(error.localizedDescription)")
            }
            self.receiveMessage()
        }
    }
    
    func close() {
        webSocketTask?.cancel()
    }
}
