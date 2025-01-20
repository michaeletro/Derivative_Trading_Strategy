#include "WebSocketServer.h"

WebSocketServer::WebSocketServer() {
    wsServer.init_asio();
    wsServer.set_open_handler([this](websocketpp::connection_hdl hdl) { onOpen(hdl); });
    wsServer.set_close_handler([this](websocketpp::connection_hdl hdl) { onClose(hdl); });
    wsServer.set_message_handler([this](websocketpp::connection_hdl hdl, server::message_ptr msg) { onMessage(hdl, msg); });
}

void WebSocketServer::onOpen(websocketpp::connection_hdl hdl) {
    std::lock_guard<std::mutex> lock(connectionsMutex);
    connections[hdl] = {};
}

void WebSocketServer::onClose(websocketpp::connection_hdl hdl) {
    std::lock_guard<std::mutex> lock(connectionsMutex);
    connections.erase(hdl);
}

void WebSocketServer::onMessage(websocketpp::connection_hdl hdl, server::message_ptr msg) {
    // Handle incoming messages (optional for now)
}

void WebSocketServer::start(uint16_t port) {
    wsServer.listen(port);
    wsServer.start_accept();
    wsServer.run();
}

void WebSocketServer::broadcast(const nlohmann::json& message) {
    std::lock_guard<std::mutex> lock(connectionsMutex);
    for (const auto& [hdl, _] : connections) {
        wsServer.send(hdl, message.dump(), websocketpp::frame::opcode::text);
    }
}
