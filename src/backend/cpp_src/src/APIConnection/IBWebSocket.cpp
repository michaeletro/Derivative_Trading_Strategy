#include "../../headers/APIConnection/IBWebSocket.h"

IBWebSocket::IBWebSocket() {
    wsClient.init_asio();

    // Accept self-signed TLS certificates (IB Client Portal uses self-signed by default).
    wsClient.set_tls_init_handler([]([[maybe_unused]] websocketpp::connection_hdl) {
        auto ctx = std::make_shared<websocketpp::lib::asio::ssl::context>(websocketpp::lib::asio::ssl::context::tlsv12);
        ctx->set_options(websocketpp::lib::asio::ssl::context::default_workarounds);
        ctx->set_verify_mode(websocketpp::lib::asio::ssl::verify_none);
        return ctx;
    });

    wsClient.set_open_handler(bind(&IBWebSocket::onOpen, this, _1));
    wsClient.set_message_handler(bind(&IBWebSocket::onMessage, this, _1, _2));
    wsClient.set_close_handler(bind(&IBWebSocket::onClose, this, _1));
    wsClient.set_fail_handler(bind(&IBWebSocket::onFail, this, _1));
}

IBWebSocket::~IBWebSocket() {
    close();
}

void IBWebSocket::connect(const std::string& uri) {
    websocketpp::lib::error_code ec;
    wsUri = uri;
    auto conn = wsClient.get_connection(uri, ec);
    
    if (ec) {
        std::cerr << "WebSocket connection failed: " << ec.message() << std::endl;
        return;
    }

    wsHandle = conn->get_handle();
    wsClient.connect(conn);
    wsThread = std::thread([this]() { wsClient.run(); });
}

void IBWebSocket::sendMessage(const std::string& message) {
    websocketpp::lib::error_code ec;
    if (!connected.load()) {
        std::cerr << "WebSocket send skipped: not connected" << std::endl;
        return;
    }

    wsClient.send(wsHandle, message, websocketpp::frame::opcode::text, ec);
    if (ec) {
        std::cerr << "WebSocket send failed: " << ec.message() << std::endl;
    }
}

void IBWebSocket::close() {
    websocketpp::lib::error_code ec;
    if (connected.load()) {
        wsClient.close(wsHandle, websocketpp::close::status::going_away, "Client shutdown", ec);
    }
    if (wsThread.joinable()) {
        wsThread.join();
    }
    connected.store(false);
}

void IBWebSocket::onOpen([[maybe_unused]] connection_hdl hdl) {
    connected.store(true);
    std::cout << "WebSocket Connection Established ✅" << std::endl;
}

void IBWebSocket::onMessage([[maybe_unused]] connection_hdl hdl, client::message_ptr msg) {
    std::cout << "Received message: " << msg->get_payload() << std::endl;
}

void IBWebSocket::onClose([[maybe_unused]] connection_hdl hdl) {
    connected.store(false);
    std::cout << "WebSocket Connection Closed ❌" << std::endl;
}

void IBWebSocket::onFail([[maybe_unused]] connection_hdl hdl) {
    connected.store(false);
    std::cout << "WebSocket Connection Failed ❌" << std::endl;
}

bool IBWebSocket::isConnected() const {
    return connected.load();
}

std::string IBWebSocket::getUri() const {
    return wsUri;
}
