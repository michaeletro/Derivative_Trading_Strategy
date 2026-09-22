#include <dts/tws_broker.hpp>
#include <iostream>
#include <thread>

int main(int argc, char** argv) {
    try {
        dts::TwsConfig config;
        for (int i = 1; i < argc; ++i) {
            const std::string key = argv[i];
            if (key == "--help") {
                std::cout << "tws_probe [--host 127.0.0.1] [--port 4002] [--client-id 17] [--timeout-ms 10000]\n"
                             "Read-only native TWS handshake; never submits orders.\n";
                return 0;
            }
            if (++i == argc) throw std::invalid_argument("Missing argument value");
            const std::string value = argv[i];
            if (key == "--host") { config.host = value; continue; }
            std::size_t used = 0;
            const int number = std::stoi(value, &used);
            if (used != value.size() || number <= 0) throw std::invalid_argument("Invalid argument");
            if (key == "--port") config.port = number;
            else if (key == "--client-id") config.client_id = number;
            else if (key == "--timeout-ms" && number <= 60000) config.timeout = std::chrono::milliseconds(number);
            else throw std::invalid_argument("Unknown argument or excessive timeout");
        }
        dts::TwsBroker broker(config);
        broker.connect();
        while (broker.state() == dts::ConnectionState::Connecting) {
            (void)broker.poll();
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        const bool ready = broker.state() == dts::ConnectionState::Ready;
        broker.disconnect();
        std::cout << (ready ? "Native TWS handshake complete; no orders.\n" : "Native TWS handshake failed or timed out.\n");
        return ready ? 0 : 1;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n'; return 2;
    }
}
