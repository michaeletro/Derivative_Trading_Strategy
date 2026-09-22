#pragma once

#include <chrono>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace dts::pricing {
inline constexpr char engine_version[] = "european-gbm-v1";
inline constexpr char rng_version[] = "mt19937_64-box-muller-open52-v1";
inline constexpr std::uint64_t min_paths = 1000;
inline constexpr std::uint64_t max_paths = 2000000;

enum class Right { Call, Put };
enum class Method { Plain, Antithetic };

// European payoff, constant continuously compounded r and q, no discrete dividends.
// All prices are per ONE payoff unit, not a listed contract or portfolio value.
struct Inputs {
    double spot = 100.0;
    double strike = 100.0;
    double maturity = 1.0; // years, explicitly supplied (no calendar inference)
    double rate = 0.05;   // decimal per year
    double dividend_yield = 0.0;
    double volatility = 0.2; // decimal annualized volatility
    Right right = Right::Call;
    void validate() const;
};
struct Config {
    std::uint64_t paths = 100000; // payoff evaluations; 2 per antithetic pair
    std::uint64_t seed = 42;
    Method method = Method::Plain;
    void validate() const;
};
struct Estimate {
    double price = 0.0;
    // null for a stochastic sample with no observed variation, NOT certainty.
    std::optional<double> standard_error;
    std::optional<double> ci_low;
    std::optional<double> ci_high;
    double sample_variance = 0.0; // variance across independent observations/pairs
};
struct Point {
    std::uint64_t paths = 0;
    std::uint64_t independent_samples = 0;
    Estimate estimate;
};
struct Result {
    double analytical = 0.0;
    Estimate estimate;
    std::uint64_t paths_evaluated = 0;
    std::uint64_t independent_samples = 0;
    std::uint64_t positive_payoffs = 0;
    bool deterministic = false;
    double runtime_ms = 0.0;
    std::vector<Point> convergence;
    std::vector<std::string> warnings;
};

double black_scholes(const Inputs& inputs);
Result simulate(const Inputs& inputs, const Config& config,
                std::chrono::milliseconds budget = std::chrono::milliseconds(5000));
std::uint64_t parse_seed(const std::string& text);
} // namespace dts::pricing
