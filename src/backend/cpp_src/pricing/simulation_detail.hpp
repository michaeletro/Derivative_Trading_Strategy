#pragma once
#include <dts/pricing.hpp>
#include <algorithm>
#include <cmath>
#include <random>
#include <stdexcept>

namespace dts::pricing::detail {
inline constexpr double z95 = 1.959963984540054;
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
} // namespace dts::pricing::detail
