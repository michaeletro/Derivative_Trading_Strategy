#pragma once
#include "pricing.hpp"
#include <array>

namespace dts::sde {
inline constexpr const char* engine_version = "coupled-gbm-convergence-1";
inline constexpr std::uint64_t maximum_work = 50000000;
struct Config {
    std::uint64_t paths = 10000, seed = 42;
    unsigned first_steps = 8, levels = 6;
    void validate() const;
    std::vector<unsigned> steps() const;
    std::uint64_t work() const;
};
// One entry per independent path (not per time step). Intervals are pointwise.
struct Statistic {
    double mean = 0, sample_variance = 0;
    std::optional<double> standard_error, ci_low, ci_high;
};
struct TracePoint { double time = 0, exact = 0, euler = 0, milstein = 0; };
struct PathLevel {
    unsigned steps = 0;
    double euler_terminal = 0, milstein_terminal = 0;
    std::array<std::uint64_t,2> nonpositive_steps{};
    // Kernel timings exclude shared Brownian generation and statistics overhead.
    std::array<double,2> kernel_ms{};
    std::vector<TracePoint> trace;
};
struct CoupledPath { double exact_terminal = 0; std::vector<PathLevel> levels; };
struct SchemeResult {
    Statistic price, absolute_terminal_error, paired_payoff_bias;
    double root_mean_squared_terminal_error = 0, kernel_ms = 0;
    std::uint64_t nonpositive_steps = 0, paths_with_nonpositive = 0, positive_payoffs = 0;
};
struct LevelResult {
    unsigned steps = 0;
    double step_years = 0;
    std::uint64_t scheme_updates = 0;
    std::array<SchemeResult,2> schemes;
    std::vector<std::vector<TracePoint>> sample_paths;
};
struct Result {
    double analytical_price = 0, runtime_ms = 0, brownian_generation_ms = 0;
    Statistic exact_price;
    std::uint64_t independent_paths = 0, paths_processed = 0, normal_draws = 0, exact_positive_payoffs = 0;
    bool deterministic = false;
    std::vector<LevelResult> levels;
    std::vector<std::string> warnings;
};
// Fine increments are aggregated pairwise down a dyadic tree, never redrawn.
// This public deterministic kernel permits independent path-by-path validation.
CoupledPath coupled_path(const pricing::Inputs&, const std::vector<double>& fine_increments,
                         const std::vector<unsigned>& steps, bool trace = false);
Result run(const pricing::Inputs&, const Config&,
           std::chrono::milliseconds budget = std::chrono::milliseconds(5000));
} // namespace dts::sde
