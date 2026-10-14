#pragma once
#include "historical.hpp"

namespace dts {
struct TickSpec {
    Contract contract;
    std::string type = "TRADES";
    bool use_rth = true;
    void validate() const {
        HistorySpec s; s.contract=contract; s.validate();
        if(type!="TRADES" && type!="BID_ASK") throw std::invalid_argument("Choose trades or best bid/ask ticks");
    }
    void validate(HistoryWindow w) const {
        validate();
        if(w.start<946684800 || w.end>4102444800LL || w.end<=w.start || w.end-w.start>31LL*86400)
            throw std::invalid_argument("Choose a start and end within a 31-day period (years 2000..2099)");
    }
};
// Provider timestamp precision is ONE SECOND. Vector order is retained, including
// identical ticks in the same second; never deduplicate on time/price/size.
struct HistoricalTick {
    std::int64_t time=0;
    double price=0, bid=0, ask=0;
    std::string size, bid_size, ask_size, exchange, conditions;
    bool past_limit=false, unreported=false, bid_past_low=false, ask_past_high=false;
    void validate(const std::string& type) const {
        if(time<946684800 || time>4102444800LL) throw std::invalid_argument("Invalid tick timestamp");
        auto price_ok=[](double n){if(!std::isfinite(n)||n<0||n>1e12)throw std::invalid_argument("Invalid tick price");};
        auto size_ok=[](const std::string& s){
            if(s.empty()||s.size()>64)throw std::invalid_argument("Invalid tick size");
            std::size_t used=0;auto n=std::stold(s,&used);
            if(used!=s.size()||!std::isfinite(n)||n<0||n>1e30)throw std::invalid_argument("Invalid tick size");
        };
        if(type=="TRADES"){price_ok(price);size_ok(size);}else{price_ok(bid);price_ok(ask);size_ok(bid_size);size_ok(ask_size);}
        for(const auto* s:{&exchange,&conditions})if(s->size()>256||s->find('\0')!=std::string::npos)throw std::invalid_argument("Invalid tick text");
    }
};
struct HistoricalTickPage {
    RequestId request_id=0;
    std::vector<HistoricalTick> ticks;
    std::string status="complete";
    int code=0;
    std::string error;
};
inline constexpr std::size_t max_tick_page=20000;
} // namespace dts
