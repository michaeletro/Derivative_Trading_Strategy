#include <dts/pricing.hpp>

#include <algorithm>
#include <charconv>
#include <cmath>
#include <limits>
#include <random>
#include <stdexcept>

namespace dts::pricing {
namespace {
using Clock = std::chrono::steady_clock;
constexpr double z95 = 1.959963984540054;
static_assert(std::numeric_limits<double>::is_iec559, "IEEE-754 doubles required");
void bounded(double x, double low, double high, const char* message) {
    if (!std::isfinite(x) || x < low || x > high) throw std::invalid_argument(message);
}
long double normal_cdf(long double x) {
    return 0.5L * std::erfc(-x / std::sqrt(2.0L));
}

// Explicit normal transform instead of implementation-defined normal_distribution.
// The engine bits and open-interval uniform mapping are specified. libm/compiler
// rounding may still differ across platforms; do not claim cross-build bit identity.
class Normals {
    std::mt19937_64 engine_;
    bool spare_ready_ = false;
    double spare_ = 0;
    double uniform_open() {
        return (static_cast<double>(engine_() >> 12) + 0.5) * 0x1p-52;
    }
public:
    explicit Normals(std::uint64_t seed) : engine_(seed) {}
    double next() {
        if (spare_ready_) { spare_ready_ = false; return spare_; }
        const double radius = std::sqrt(-2.0 * std::log(uniform_open()));
        const double angle = 6.2831853071795864769 * uniform_open();
        spare_ = radius * std::sin(angle);
        spare_ready_ = true;
        return radius * std::cos(angle);
    }
};
struct Moments {
    std::uint64_t count = 0;
    double mean = 0, m2 = 0;
    void add(double x) {
        if (!std::isfinite(x)) throw std::overflow_error("Nonfinite simulated payoff");
        ++count;
        const double delta = x - mean;
        mean += delta / static_cast<double>(count);
        m2 += delta * (x - mean);
    }
    Estimate estimate() const {
        Estimate out;
        out.price = mean;
        if (count < 2) return out;
        out.sample_variance = std::max(0.0, m2 / static_cast<double>(count - 1));
        // A zero observed variance in a stochastic sample does not establish
        // zero population variance: deep OTM samples may miss the whole tail.
        if (out.sample_variance > 0) {
            const double se = std::sqrt(out.sample_variance / static_cast<double>(count));
            out.standard_error = se;
            out.ci_low = mean - z95 * se;
            out.ci_high = mean + z95 * se;
        }
        return out;
    }
};
} // namespace

void Inputs::validate() const {
    bounded(spot, 0, 1e8, "Spot must be between 0 and 100000000");
    bounded(strike, 1e-8, 1e8, "Strike must be between 1e-8 and 100000000");
    bounded(maturity, 0, 30, "Maturity must be between 0 and 30 years");
    bounded(rate, -0.5, 0.5, "Continuous rate must be between -0.5 and 0.5");
    bounded(dividend_yield, -0.5, 0.5, "Continuous yield must be between -0.5 and 0.5");
    bounded(volatility, 0, 3, "Volatility must be between 0 and 3");
    if (volatility * std::sqrt(maturity) > 3)
        throw std::invalid_argument("Total log-return standard deviation must not exceed 3");
    if (right != Right::Call && right != Right::Put)
        throw std::invalid_argument("Only European calls and puts are supported");
}
void Config::validate() const {
    if (paths < min_paths || paths > max_paths)
        throw std::invalid_argument("Paths must be an integer between 1000 and 2000000");
    if (method != Method::Plain && method != Method::Antithetic)
        throw std::invalid_argument("Unknown Monte Carlo method");
    if (method == Method::Antithetic && paths % 2)
        throw std::invalid_argument("Antithetic paths must be even (two payoffs per pair)");
}
std::uint64_t parse_seed(const std::string& text) {
    if (text.empty() || text.size() > 20 || text.find_first_not_of("0123456789") != std::string::npos)
        throw std::invalid_argument("Seed must be an unsigned 64-bit decimal string");
    std::uint64_t value = 0;
    const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
    if (result.ec != std::errc{} || result.ptr != text.data() + text.size())
        throw std::invalid_argument("Seed exceeds unsigned 64-bit range");
    return value;
}

double black_scholes(const Inputs& x) {
    x.validate();
    const long double s = static_cast<long double>(x.spot) * std::exp(-static_cast<long double>(x.dividend_yield) * x.maturity);
    const long double k = static_cast<long double>(x.strike) * std::exp(-static_cast<long double>(x.rate) * x.maturity);
    const bool call = x.right == Right::Call;
    if (x.spot == 0 || x.maturity == 0 || x.volatility == 0)
        return static_cast<double>(std::max(call ? s-k : k-s, 0.0L));
    const long double v = static_cast<long double>(x.volatility) * std::sqrt(static_cast<long double>(x.maturity));
    const long double d1 = (std::log(static_cast<long double>(x.spot)) - std::log(static_cast<long double>(x.strike))
        + (static_cast<long double>(x.rate) - x.dividend_yield) * x.maturity) / v + v / 2;
    const long double d2 = d1 - v;
    // Price the out-of-the-money side directly; obtain the ITM side via parity.
    long double c, p;
    if (s >= k) {
        p = std::max(k * normal_cdf(-d2) - s * normal_cdf(-d1), 0.0L);
        c = (s - k) + p;
    } else {
        c = std::max(s * normal_cdf(d1) - k * normal_cdf(d2), 0.0L);
        p = (k - s) + c;
    }
    return static_cast<double>(call ? c : p);
}

Result simulate(const Inputs& x, const Config& config, std::chrono::milliseconds budget) {
    x.validate(); config.validate();
    if (budget.count() <= 0) throw std::invalid_argument("Runtime budget must be positive");
    const auto started = Clock::now();
    Result out;
    out.analytical = black_scholes(x);
    out.deterministic = x.spot == 0 || x.volatility == 0 || x.maturity == 0;
    if (out.deterministic) {
        out.estimate = {out.analytical, 0.0, out.analytical, out.analytical, 0.0};
        out.warnings.emplace_back("Deterministic model limit: no random paths were evaluated; the interval is exact under these assumptions.");
    } else {
        Normals normals(config.seed);
        Moments moments;
        const bool paired = config.method == Method::Antithetic;
        const std::uint64_t factor = paired ? 2 : 1;
        const auto independent = config.paths / factor;
        const double v = x.volatility * std::sqrt(x.maturity);
        const double log_discounted_spot = std::log(x.spot) - x.dividend_yield * x.maturity - 0.5 * v * v;
        const double discounted_strike = x.strike * std::exp(-x.rate * x.maturity);
        const auto payoff = [&](double z) {
            const double discounted_terminal = std::exp(log_discounted_spot + v * z);
            const double value = std::max(x.right == Right::Call ? discounted_terminal-discounted_strike : discounted_strike-discounted_terminal, 0.0);
            if (value > 0) ++out.positive_payoffs;
            return value;
        };
        std::uint64_t checkpoint = 100;
        for (std::uint64_t i = 1; i <= independent; ++i) {
            if ((i & 4095U) == 0 && Clock::now() - started > budget)
                throw std::runtime_error("Pricing runtime budget exceeded; no partial result accepted");
            const double z = normals.next();
            const double first = payoff(z);
            // Independent pair averages, not the correlated individual payoffs,
            // are observations for Welford variance and standard error.
            moments.add(paired ? first / 2 + payoff(-z) / 2 : first);
            if (i == checkpoint || i == independent) {
                out.convergence.push_back({i * factor, i, moments.estimate()});
                checkpoint *= 2;
            }
        }
        out.paths_evaluated = config.paths;
        out.independent_samples = independent;
        out.estimate = moments.estimate();
        if (!out.estimate.standard_error)
            out.warnings.emplace_back("No sample variation observed. Sampling uncertainty is unresolved, not zero; the confidence interval is withheld.");
        if (out.positive_payoffs < 30)
            out.warnings.emplace_back("Fewer than 30 positive payoffs: rare-event sampling may be unreliable, even with a nominal interval.");
        if (v > 1)
            out.warnings.emplace_back("High total volatility: lognormal tails can make normal-approximation intervals unreliable at finite sample sizes.");
    }
    out.runtime_ms = std::chrono::duration<double, std::milli>(Clock::now() - started).count();
    return out;
}
} // namespace dts::pricing
