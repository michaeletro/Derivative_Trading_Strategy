#pragma once
#include "domain.hpp"
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <string>

namespace dts {
struct RecordingClockSample {
    std::int64_t wall_ns = 0, monotonic_ns = 0, read_span_ns = 0;
    static RecordingClockSample now() {
        const auto before = Clock::now();
        const auto wall = std::chrono::system_clock::now();
        const auto after = Clock::now();
        return {std::chrono::duration_cast<std::chrono::nanoseconds>(wall.time_since_epoch()).count(),
                std::chrono::duration_cast<std::chrono::nanoseconds>(after.time_since_epoch()).count(),
                std::chrono::duration_cast<std::chrono::nanoseconds>(after - before).count()};
    }
};
struct RecordingClockStatus {
    std::string state = "checking";
    std::uint64_t samples = 0, wall_regressions = 0, monotonic_regressions = 0, sampling_gaps = 0;
    double wall_elapsed_seconds = 0, monotonic_elapsed_seconds = 0, continuous_seconds = 0;
    double max_divergence_seconds = 0, max_read_span_seconds = 0, max_sampling_gap_seconds = 0;
    double last_sample_age_seconds = 0;
};
// Process-local, constant-memory diagnostic. The application serializes access.
// Failure is sticky for this process; neither reconnecting nor refreshing clears
// it. This never adjusts a clock, mutates a capture, or certifies UTC accuracy.
class RecordingClockMonitor {
public:
    static constexpr double required_seconds = 90, threshold_seconds = 1, max_gap_seconds = 2;
    void observe(const RecordingClockSample& sample) {
        if (!status_.samples) { first_ = sample; continuous_start_ = sample.monotonic_ns; }
        else {
            if (sample.wall_ns < last_.wall_ns) ++status_.wall_regressions;
            if (sample.monotonic_ns < last_.monotonic_ns) ++status_.monotonic_regressions;
            const auto gap = seconds(sample.monotonic_ns, last_.monotonic_ns);
            status_.max_sampling_gap_seconds = std::max(status_.max_sampling_gap_seconds, gap);
            if (gap > max_gap_seconds || gap < 0) {
                ++status_.sampling_gaps; continuous_start_ = sample.monotonic_ns;
            }
        }
        ++status_.samples;
        status_.wall_elapsed_seconds = seconds(sample.wall_ns, first_.wall_ns);
        status_.monotonic_elapsed_seconds = seconds(sample.monotonic_ns, first_.monotonic_ns);
        status_.continuous_seconds = seconds(sample.monotonic_ns, continuous_start_);
        status_.max_divergence_seconds = std::max(status_.max_divergence_seconds,
            std::abs(status_.wall_elapsed_seconds - status_.monotonic_elapsed_seconds));
        status_.max_read_span_seconds = std::max(status_.max_read_span_seconds, seconds(sample.read_span_ns, 0));
        // A heavily interrupted clock read cannot establish a clean interval.
        if (sample.read_span_ns < 0 || sample.read_span_ns > 50000000) {
            if (sample.read_span_ns < 0) ++status_.monotonic_regressions;
            continuous_start_ = sample.monotonic_ns; status_.continuous_seconds = 0;
        }
        last_ = sample;
    }
    RecordingClockStatus status(std::int64_t now_monotonic_ns) const {
        auto out = status_;
        out.last_sample_age_seconds = out.samples ? seconds(now_monotonic_ns, last_.monotonic_ns) : 0;
        if (out.max_divergence_seconds > threshold_seconds || out.wall_regressions || out.monotonic_regressions)
            out.state = "inconsistent";
        else if (!out.samples || out.continuous_seconds < required_seconds)
            out.state = "checking";
        else if (out.last_sample_age_seconds < 0 || out.last_sample_age_seconds > max_gap_seconds)
            out.state = "sampling_gap";
        else out.state = "consistent_during_check";
        return out;
    }
private:
    static double seconds(std::int64_t a, std::int64_t b) {
        return static_cast<double>((static_cast<long double>(a) - static_cast<long double>(b)) / 1e9L);
    }
    RecordingClockSample first_, last_;
    std::int64_t continuous_start_ = 0;
    RecordingClockStatus status_;
};
} // namespace dts
