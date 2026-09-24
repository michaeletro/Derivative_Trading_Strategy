#pragma once
#include "pricing.hpp"

namespace dts::pricing {
inline constexpr char sensitivity_version[] = "european-greeks-scenarios-v1";

// Mathematical derivatives per payoff unit. Theta is -dV/d(tau) per YEAR;
// vega/rho are per UNIT decimal volatility/rate, never per percentage point.
struct Greeks {
    double price = 0;
    std::optional<double> delta, gamma, vega, theta, rho;
    std::string status = "unavailable";
    std::string reason;
};
Greeks analytical_greeks(const Inputs& inputs);

enum class SensitivityMethod { Pathwise, CentralCRN };
struct SensitivityConfig {
    // Config.paths counts base terminal draws here, not bumped payoff evaluations.
    Config sampling;
    SensitivityMethod estimator = SensitivityMethod::Pathwise;
    double relative_spot_bump = 0.001;
    double volatility_bump = 0.001; // ABSOLUTE decimal annualized volatility
    void validate() const;
};
struct SensitivityResult {
    bool available = false;
    std::string reason;
    Estimate delta, vega; // Estimate.price is the derivative estimate in raw units.
    double delta_target = 0, vega_target = 0;
    // CRN sampling intervals target the finite difference, NOT the exact derivative.
    double delta_fd_bias = 0, vega_fd_bias = 0;
    std::uint64_t terminal_draws = 0, independent_samples = 0;
    std::uint64_t bumped_payoff_evaluations = 0, active_base_paths = 0;
    double runtime_ms = 0;
    std::vector<std::string> warnings;
};
SensitivityResult simulate_greeks(const Inputs&, const SensitivityConfig&,
    std::chrono::milliseconds budget = std::chrono::milliseconds(5000));

struct Shock {
    double relative_spot = 0; // 0.10 means a +10% spot shock
    double volatility = 0;   // 0.01 means +1 volatility percentage point
    double elapsed_years = 0; // Time roll forward, 0 <= elapsed <= maturity
};
struct Scenario {
    Shock shock;
    bool valid = false;
    std::string reason;
    std::optional<double> shocked_price, full_change, approximation, residual;
};
Scenario reprice_scenario(const Inputs&, const Shock&);
struct CurvePoint { double spot = 0; Greeks greeks; bool valid = true; };
// Deterministic BSM calculations; no random draws and no trading/account inputs.
std::vector<Scenario> scenario_grid(const Inputs&, double spot_span,
    double volatility_span, double elapsed_years); // 21 spots x 11 vol shocks
std::vector<CurvePoint> spot_curve(const Inputs&, double spot_span); // 41 points
} // namespace dts::pricing
