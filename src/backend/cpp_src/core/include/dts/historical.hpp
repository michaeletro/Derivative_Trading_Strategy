#pragma once
#include "domain.hpp"
#include <algorithm>
#include <ctime>
#include <iomanip>
#include <limits>
#include <sstream>
#include <vector>

namespace dts {
// Intraday times are UTC epoch seconds. Daily range endpoints are synthetic
// UTC-midnight coordinates of provider SESSION DATES, not exchange timestamps.
struct HistoryWindow { std::int64_t start = 0, end = 0; }; // [start,end)
struct HistorySpec {
    Contract contract;
    std::string bar_size = "1 day", price_type = "TRADES";
    bool use_rth = true;
    void validate() const {
        contract.validate();
        if (contract.security_type != SecurityType::Equity || contract.option ||
            contract.currency != "USD" || contract.id > std::numeric_limits<int>::max())
            throw std::invalid_argument("Historical manager supports resolved USD STK contracts only (equities/ETFs)");
        if (bar_size != "1 day" && bar_size != "1 min")
            throw std::invalid_argument("Historical interval must be 1 day or 1 min");
        if (price_type != "TRADES" && price_type != "MIDPOINT" && price_type != "BID" && price_type != "ASK")
            throw std::invalid_argument("Historical price type must be TRADES, MIDPOINT, BID or ASK");
    }
    void validate(HistoryWindow w) const {
        validate();
        const auto unit = bar_size == "1 day" ? 86400 : 60;
        const auto maximum = bar_size == "1 day" ? 366LL*86400 : 86400LL;
        if (w.start < 946684800 || w.end > 4102444800LL || w.end <= w.start ||
            w.end-w.start > maximum || w.start%unit || w.end%unit)
            throw std::invalid_argument("Use aligned half-open dates/times: max 366 daily dates or 24 hours of minute bars, years 2000..2099");
    }
};
struct HistoricalBar {
    std::string time; // Exact provider time: YYYYMMDD (daily) or epoch seconds.
    double open=0, high=0, low=0, close=0;
    std::optional<std::string> volume; // Exact provider decimal; absent is not zero.
    std::optional<double> wap;
    std::optional<int> count;
    void validate() const {
        if (time.empty() || time.size()>32 || time.find_first_not_of("0123456789")!=std::string::npos)
            throw std::invalid_argument("Unexpected historical bar time encoding");
        for (double p : {open,high,low,close})
            if (!std::isfinite(p) || p<0 || p>1e12) throw std::invalid_argument("Invalid historical OHLC value");
        if (low>high || open<low || open>high || close<low || close>high)
            throw std::invalid_argument("Inconsistent historical OHLC bounds");
        if (volume) {
            if (volume->empty() || volume->size()>64) throw std::invalid_argument("Invalid historical volume");
            std::size_t used=0;const auto v=std::stold(*volume,&used);
            if (used!=volume->size() || !std::isfinite(v) || v<0)
                throw std::invalid_argument("Invalid historical volume");
        }
        if (wap && (!std::isfinite(*wap) || *wap<0 || *wap>1e12)) throw std::invalid_argument("Invalid historical WAP");
        if (count && *count<0) throw std::invalid_argument("Invalid historical count");
    }
};
struct HistoricalBarEvent { RequestId request_id; HistoricalBar bar; };
struct HistoricalEnd {
    RequestId request_id;
    std::string status = "complete"; // complete, failed, unavailable, interrupted
    int code = 0;
    std::string provider_start, provider_end;
};
inline std::string utc_text(std::int64_t seconds, const char* format="%Y%m%d-%H:%M:%S") {
    const auto t=static_cast<std::time_t>(seconds);std::tm tm{};
    #ifdef _WIN32
    if (gmtime_s(&tm,&t)) throw std::invalid_argument("UTC time outside supported range");
#else
    if (!gmtime_r(&t,&tm)) throw std::invalid_argument("UTC time outside supported range");
#endif
    std::ostringstream out;out<<std::put_time(&tm,format);return out.str();
}
inline std::int64_t bar_coordinate(const HistorySpec& spec,const HistoricalBar& b) {
    b.validate();
    if (spec.bar_size=="1 min") {
        std::size_t n=0;const auto t=std::stoll(b.time,&n);
        if (n!=b.time.size()||t<946684800||t>4102444800LL||t%60)throw std::invalid_argument("Invalid intraday UTC epoch/minute alignment");
        return t;
    }
    if(b.time.size()!=8)throw std::invalid_argument("Daily bars must contain provider YYYYMMDD dates");
    std::tm tm{};std::istringstream in(b.time);in>>std::get_time(&tm,"%Y%m%d");
    if(in.fail())throw std::invalid_argument("Invalid daily session date");
    #ifdef _WIN32
    const auto t=_mkgmtime(&tm);
#else
    const auto t=timegm(&tm);
#endif
    if(t<946684800||t>4102444800LL||utc_text(t,"%Y%m%d")!=b.time)throw std::invalid_argument("Invalid daily date");
    return t;
}
inline std::vector<HistoryWindow> uncovered(HistoryWindow target,std::vector<HistoryWindow> covered) {
    std::sort(covered.begin(),covered.end(),[](auto a,auto b){return a.start<b.start;});
    std::vector<HistoryWindow> out;auto cursor=target.start;
    for(auto w:covered) {
        if(w.end<=cursor || w.start>=target.end)continue;
        if(w.start>cursor)out.push_back({cursor,std::min(w.start,target.end)});
        cursor=std::max(cursor,std::min(w.end,target.end));
    }
    if(cursor<target.end)out.push_back({cursor,target.end});
    return out;
}
inline std::vector<HistoryWindow> historical_chunks(const HistorySpec& s,const std::vector<HistoryWindow>& gaps) {
    std::vector<HistoryWindow> out;
    const std::int64_t step=s.bar_size=="1 day" ? 30*86400 : 86400;
    for(auto g:gaps)for(auto t=g.start;t<g.end;t+=step)out.push_back({t,std::min(t+step,g.end)});
    if(out.size()>32)throw std::length_error("Too many uncovered intervals; narrow the range");
    return out;
}
inline std::string history_end_time(const HistorySpec& s,HistoryWindow w) {
    // Daily responses are session dates. A two-day request buffer avoids dropping
    // a session through timezone/calendar interpretation; only target dates persist.
    return utc_text(w.end+(s.bar_size=="1 day" ? 2*86400 : 0));
}
inline std::string history_duration(const HistorySpec& s,HistoryWindow w) {
    return s.bar_size=="1 day" ? std::to_string((w.end-w.start)/86400+4)+" D" : std::to_string(w.end-w.start)+" S";
}
} // namespace dts
