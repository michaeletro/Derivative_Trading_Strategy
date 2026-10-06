#include <dts/recording_clock.hpp>
#include <iostream>
#include <stdexcept>
#define CHECK(x) do { if (!(x)) throw std::runtime_error(#x); } while (false)
namespace {
constexpr std::int64_t second = 1000000000;
dts::RecordingClockSample sample(std::int64_t seconds, std::int64_t offset = 0) {
    return {1700000000000000000LL + seconds * second + offset, seconds * second, 1000};
}
void observe(dts::RecordingClockMonitor& monitor, int start, int end) {
    for (int i = start; i <= end; ++i) monitor.observe(sample(i));
}
}
int main() {
    try {
        dts::RecordingClockMonitor stable;
        CHECK(stable.status(0).state == "checking");
        observe(stable, 0, 89);
        CHECK(stable.status(89 * second).state == "checking");
        stable.observe(sample(90));
        CHECK(stable.status(90 * second).state == "consistent_during_check");
        CHECK(stable.status(93 * second).state == "sampling_gap");
        CHECK(stable.status(89 * second).state == "sampling_gap");
        CHECK(stable.status(90 * second).max_divergence_seconds == 0);

        dts::RecordingClockMonitor drift;
        observe(drift, 0, 90);
        drift.observe(sample(91, second));
        CHECK(drift.status(91 * second).state == "consistent_during_check");
        drift.observe(sample(92, second + 1));
        CHECK(drift.status(92 * second).state == "inconsistent");
        drift.observe(sample(94)); // A temporary excursion cannot erase failure.
        CHECK(drift.status(94 * second).state == "inconsistent");
        CHECK(drift.status(94 * second).max_divergence_seconds > 1);

        dts::RecordingClockMonitor regression;
        regression.observe(sample(1)); regression.observe(sample(1, -1));
        CHECK(regression.status(second).wall_regressions == 1);
        CHECK(regression.status(second).state == "inconsistent");
        regression.observe(sample(0));
        CHECK(regression.status(0).monotonic_regressions == 1);

        dts::RecordingClockMonitor gap;
        observe(gap, 0, 90); gap.observe(sample(94));
        CHECK(gap.status(94 * second).state == "checking");
        CHECK(gap.status(94 * second).sampling_gaps == 1);
        observe(gap, 95, 184);
        CHECK(gap.status(184 * second).state == "consistent_during_check");
        auto interrupted = sample(185); interrupted.read_span_ns = 60000000;
        gap.observe(interrupted);
        CHECK(gap.status(185 * second).state == "checking");
        CHECK(gap.status(185 * second).max_read_span_seconds == .06);
        interrupted = sample(186); interrupted.read_span_ns = -1;
        gap.observe(interrupted);
        CHECK(gap.status(186 * second).state == "inconsistent");

        const auto real = dts::RecordingClockSample::now();
        CHECK(real.wall_ns > 0 && real.monotonic_ns > 0);
        std::cout << "Receipt clock warmup, sticky drift, regressions, sampling gaps and real clock reads passed\n";
        return 0;
    } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
