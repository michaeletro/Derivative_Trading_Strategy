#include <dts/replay.hpp>
#include <deque>
#include <set>

namespace dts::research {
namespace {
void field(std::ostream& out, const std::string& value) { out<<value.size()<<':'<<value; }
void value(std::ostream& out, const std::optional<double>& v) { if(v)out<<'V'<<std::hexfloat<<*v<<';';else out<<"N;"; }
RollingPoint rolling(const std::deque<double>& returns, int window, double annualization) {
    RollingPoint result;result.window=window;result.observations=std::min(returns.size(),static_cast<std::size_t>(window));
    if (result.observations!=static_cast<std::size_t>(window)) return result;
    long double mean=0,m2=0;std::size_t count=0;
    for(auto i=returns.size()-window;i<returns.size();++i) {
        const long double x=returns[i];const auto delta=x-mean;mean+=delta/++count;m2+=delta*(x-mean);
    }
    result.mean_log_return=static_cast<double>(mean);
    result.annualized_volatility=static_cast<double>(std::sqrt(std::max(0.0L,m2)/(window-1)*annualization));
    return result;
}
}
void Snapshot::validate() const {
    spec.validate(window);
    if(observations.empty()||observations.size()>maximum_bars||dataset_id<=0||request_cutoff<=0)
        throw std::invalid_argument("A research snapshot requires 1..2000 saved bars and valid provenance");
    if(time_basis!=(spec.bar_size=="1 day"?"provider_session_date":"UTC_epoch_seconds")||source.empty()||adjustment_policy.empty())
        throw std::invalid_argument("Unrecognized snapshot time/provenance conventions");
    std::int64_t last=window.start-1;
    for(const auto& row:observations) {
        if(row.coordinate_s<=last||row.coordinate_s<window.start||row.coordinate_s>=window.end||row.version_id<=0||row.request_id<=0||row.request_id>request_cutoff)
            throw std::invalid_argument("Snapshot rows must be unique, ordered, inside the window and provenance-bound");
        if(bar_coordinate(spec,row.bar)!=row.coordinate_s)throw std::invalid_argument("Snapshot source time mismatch");
        last=row.coordinate_s;
    }
    last=window.start;
    for(auto gap:uncovered_intervals) {
        if(gap.start<last||gap.start>=gap.end||gap.end>window.end)throw std::invalid_argument("Invalid frozen response-coverage intervals");
        last=gap.end;
    }
}
void ReplayConfig::validate() const {
    if(windows.empty()||windows.size()>4||!std::isfinite(annualization_factor)||annualization_factor<1||annualization_factor>10000000)
        throw std::invalid_argument("Use 1..4 rolling windows and annualization factor 1..10000000");
    int previous=1;
    for(int w:windows){if(w<2||w>250||w<=previous)throw std::invalid_argument("Rolling windows must be ascending, unique integers from 2 to 250");previous=w;}
}
std::string config_key(const ReplayConfig& c) {
    c.validate();std::ostringstream s;s.imbue(std::locale::classic());s<<engine_version<<';'<<std::hexfloat<<c.annualization_factor<<';';
    for(int w:c.windows) s<<w<<';';
    return s.str();
}
std::string snapshot_fingerprint(const Snapshot& s) {
    s.validate();std::ostringstream out;out.imbue(std::locale::classic());out<<"dts-snapshot-1;"<<s.dataset_id<<';'<<s.request_cutoff<<';'<<s.window.start<<';'<<s.window.end<<';';
    for(const auto& text:{s.source,s.time_basis,s.adjustment_policy,s.spec.contract.symbol,s.spec.contract.exchange,s.spec.contract.currency,s.spec.bar_size,s.spec.price_type})field(out,text);
    out<<s.spec.contract.id<<';'<<s.spec.use_rth<<';';
    out<<s.uncovered_intervals.size()<<';';for(auto w:s.uncovered_intervals)out<<w.start<<';'<<w.end<<';';
    out<<s.observations.size()<<';';
    for(const auto& row:s.observations) {
        out<<row.version_id<<';'<<row.request_id<<';'<<row.coordinate_s<<';'<<row.observed_ms<<';'<<row.response_finished_ms<<';';field(out,row.bar.time);
        for(double p:{row.bar.open,row.bar.high,row.bar.low,row.bar.close})out<<std::hexfloat<<p<<';';
        out<<(row.bar.volume?'V':'N');if(row.bar.volume)field(out,*row.bar.volume);
        value(out,row.bar.wap);out<<(row.bar.count?'V':'N');if(row.bar.count)out<<*row.bar.count;out<<';';
    }
    return sha256(out.str());
}
Quality inspect(const Snapshot& snapshot) {
    snapshot.validate();Quality q;q.bars=snapshot.observations.size();q.response_coverage_complete=snapshot.uncovered_intervals.empty();
    const bool minute=snapshot.spec.bar_size=="1 min";
    for(std::size_t i=0;i<q.bars;++i) {
        const auto& r=snapshot.observations[i];if(r.bar.close<=0)++q.nonpositive_closes;
        if(i){const auto& p=snapshot.observations[i-1];if(r.coordinate_s-p.coordinate_s!=(minute?60:86400))++q.discontinuities;
            if(r.bar.close>0&&p.bar.close>0&&std::abs(std::log(r.bar.close)-std::log(p.bar.close))>std::log(1.5))++q.large_return_candidates;}
    }
    q.warnings={"Retrospective frozen provider data, NOT historical point-in-time availability or a trading backtest.","Provider-native corporate-action adjustments are not normalized; returns are not total returns.","No exchange calendar or gap-free market coverage has been verified."};
    q.warnings.push_back(minute?"Minute bars release at start+60 seconds by modeling convention; actual delivery latency is unknown. Returns reset across nonconsecutive minutes.":"Daily replay uses completed-session ordinal order, not inferred exchange timestamps. Returns use adjacent supplied session closes, even across calendar gaps.");
    if(!q.response_coverage_complete)q.warnings.push_back("This snapshot includes intervals without completed provider responses.");
    if(q.nonpositive_closes)q.warnings.push_back("Nonpositive closes reset rolling-return windows; prices are not replaced or interpolated.");
    if(q.large_return_candidates)q.warnings.push_back("Large adjacent price changes require review; no corporate action is inferred or corrected.");
    return q;
}
ReplayResult replay(const Snapshot& snapshot,const ReplayConfig& config,std::size_t count) {
    // Validate the immutable manifest once, then use only the requested prefix.
    snapshot.validate();config.validate();
    if(count>snapshot.observations.size())throw std::invalid_argument("Replay cursor exceeds the frozen snapshot");
    const bool minute=snapshot.spec.bar_size=="1 min";
    ReplayResult out;out.processed=count;out.total=snapshot.observations.size();
    out.availability_policy=minute?"bar_start_plus_60s_modeled_no_delivery_latency":"completed_session_ordinal_no_exchange_timestamp";
    out.return_policy=minute?"consecutive_60s_only_reset_across_gaps":"adjacent_observed_session_closes_no_calendar_validation";
    std::deque<double> returns;std::size_t segment=0;std::optional<double> previous;
    for(std::size_t i=0;i<count;++i) {
        const auto& bar=snapshot.observations[i];ReplayPoint point;point.ordinal=i+1;point.coordinate_s=bar.coordinate_s;point.close=bar.bar.close;
        if(minute)point.available_s=bar.coordinate_s+60;
        const bool positive=point.close>0;
        const auto span=i?bar.coordinate_s-snapshot.observations[i-1].coordinate_s:0;
        if(i)point.return_span_seconds=span;
        if(!positive) {returns.clear();previous.reset();++segment;point.status="nonpositive_close_reset";}
        else if(!previous) {point.status="initial_or_reset_observation";}
        else if(minute&&span!=60) {returns.clear();++segment;point.status="nonconsecutive_minute_reset";}
        else {point.log_return=std::log(point.close)-std::log(*previous);returns.push_back(*point.log_return);
            if(returns.size()>static_cast<std::size_t>(config.windows.back()))returns.pop_front();
            point.status=(!minute&&span!=86400)?"adjacent_observation_calendar_span_unverified":"observed_return";}
        if(positive)previous=point.close;
        point.segment=segment;
        for(int window:config.windows)point.rolling.push_back(rolling(returns,window,config.annualization_factor));
        out.points.push_back(std::move(point));
    }
    return out;
}
} // namespace dts::research
