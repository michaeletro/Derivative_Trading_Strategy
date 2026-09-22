#include <dts/pricing.hpp>
#include <iomanip>
#include <iostream>
#include <stdexcept>

int main(int argc, char** argv) {
    try {
        dts::pricing::Inputs x;
        dts::pricing::Config c;
        for (int i = 1; i < argc; ++i) {
            const std::string flag = argv[i];
            if (flag == "--help") {
                std::cout << "Offline European BSM / exact-GBM research demo; no broker calls.\n"
                    "--spot --strike --maturity --rate --dividend --volatility (decimal rates)\n"
                    "--right call|put --method plain|antithetic --paths 100000 --seed 42\n";
                return 0;
            }
            if (++i == argc) throw std::invalid_argument("Missing argument value");
            const std::string v = argv[i];
            if (flag == "--seed") c.seed = dts::pricing::parse_seed(v);
            else if (flag == "--paths") c.paths = dts::pricing::parse_seed(v);
            else if (flag == "--right") {
                if (v != "call" && v != "put") throw std::invalid_argument("Invalid right");
                x.right = v == "call" ? dts::pricing::Right::Call : dts::pricing::Right::Put;
            } else if (flag == "--method") {
                if (v != "plain" && v != "antithetic") throw std::invalid_argument("Invalid method");
                c.method = v == "plain" ? dts::pricing::Method::Plain : dts::pricing::Method::Antithetic;
            } else {
                std::size_t used = 0; const auto n = std::stod(v, &used);
                if (used != v.size()) throw std::invalid_argument("Invalid numeric argument");
                if (flag == "--spot") x.spot = n;
                else if (flag == "--strike") x.strike = n;
                else if (flag == "--maturity") x.maturity = n;
                else if (flag == "--rate") x.rate = n;
                else if (flag == "--dividend") x.dividend_yield = n;
                else if (flag == "--volatility") x.volatility = n;
                else throw std::invalid_argument("Unknown argument");
            }
        }
        const auto r = dts::pricing::simulate(x,c);
        std::cout << std::setprecision(17) << "OFFLINE MODEL SCENARIO / EUROPEAN / PER PAYOFF UNIT\n"
            << "Analytical: " << r.analytical << "\nMonte Carlo: " << r.estimate.price
            << "\nPaths evaluated: " << r.paths_evaluated << "\nIndependent observations: " << r.independent_samples << '\n';
        if (r.estimate.standard_error) std::cout << "Standard error: " << *r.estimate.standard_error
            << "\n95% sampling interval: [" << *r.estimate.ci_low << ", " << *r.estimate.ci_high << "]\n";
        else std::cout << "Sampling interval unresolved (not zero uncertainty).\n";
        for (const auto& w : r.warnings) std::cout << "Warning: " << w << '\n';
        return 0;
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
