#pragma once
#include "domain.hpp"
#include <algorithm>
#include <array>
#include <map>
#include <set>
#include <tuple>
#include <variant>
#include <vector>

namespace dts {
enum class SnapshotStatus { Unavailable, Pending, Complete, Failed };
enum class TradingMonitorState { Unavailable, Pending, Active, Failed, Stopped };
enum class TradingSection { Positions, AccountValues, OpenOrders, Executions };
constexpr std::size_t trading_row_limit = 10000;
inline bool trading_text(const std::string& s, std::size_t limit = 128, bool empty = false) {
    return (empty || !s.empty()) && s.size() <= limit &&
        std::none_of(s.begin(), s.end(), [](unsigned char c) { return c < 32 || c == 127; });
}
inline bool trading_account(const std::string& s) {
    return trading_text(s,64) && std::all_of(s.begin(),s.end(),[](unsigned char c) {
        return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.';
    });
}
inline bool trading_decimal(const std::string& s) {
    if (!trading_text(s,64)) return false;
    std::size_t i=0;
    if(s[i]=='-' || s[i]=='+') ++i;
    bool digits=false;
    while(i<s.size() && s[i]>='0' && s[i]<='9') { digits=true; ++i; }
    if(i<s.size() && s[i]=='.') {
        ++i; while(i<s.size() && s[i]>='0' && s[i]<='9') { digits=true; ++i; }
    }
    if(!digits) return false;
    if(i<s.size() && (s[i]=='e' || s[i]=='E')) {
        ++i; if(i<s.size() && (s[i]=='+' || s[i]=='-')) ++i;
        const auto begin=i; while(i<s.size() && s[i]>='0' && s[i]<='9') ++i;
        if(i==begin) return false;
    }
    if(i!=s.size()) return false;
    try { return std::isfinite(std::stod(s)); } catch(const std::exception&) { return false; }
}
struct TradingContract {
    ContractId con_id = 0;
    std::string symbol, security_type, exchange, currency;
    bool valid() const { return con_id>0 && trading_text(symbol) && trading_text(security_type,16) &&
        trading_text(exchange,128,true) && trading_text(currency,16,true); }
};
struct TradingPosition {
    TradingContract contract;
    std::string quantity;
    std::optional<double> average_cost;
    std::string model_code = {};
    bool valid() const { return contract.valid() && trading_decimal(quantity) &&
        (!average_cost || std::isfinite(*average_cost)) && trading_text(model_code,64,true); }
};
struct TradingAccountValue {
    std::string tag,value,currency;
    bool valid() const { return trading_text(tag,64) && trading_text(value,256) && trading_text(currency,16,true); }
};
struct TradingOpenOrder {
    TradingContract contract;
    int order_id=0,client_id=0;
    std::int64_t perm_id=0;
    std::string action,order_type,quantity,status,order_ref;
    std::optional<double> limit_price;
    std::optional<std::string> filled,remaining; // Unknown in this snapshot; never inferred.
    bool valid() const { return contract.valid() && perm_id>=0 &&
        trading_text(action,16) && trading_text(order_type,32) && trading_decimal(quantity) &&
        trading_text(status,64,true) && trading_text(order_ref,128,true) &&
        (!limit_price || std::isfinite(*limit_price)); }
};
struct TradingExecution {
    TradingContract contract;
    std::string exec_id,time,side,quantity;
    int order_id=0,client_id=0;
    std::int64_t perm_id=0;
    std::optional<double> price;
    bool valid() const { return contract.valid() && trading_text(exec_id) && trading_text(time,64) &&
        trading_text(side,16) && trading_decimal(quantity) && perm_id>=0 &&
        (!price || std::isfinite(*price)); }
};
struct ManagedAccountsEvent { bool success=false; std::vector<std::string> accounts; };
struct TradingSectionEnd { bool success=true; int error_code=0; };
using TradingMonitorPayload=std::variant<TradingPosition,TradingAccountValue,TradingOpenOrder,TradingExecution,TradingSectionEnd>;
struct TradingMonitorEvent {
    RequestId request_id=0;
    TradingSection section=TradingSection::Positions;
    TradingMonitorPayload value=TradingSectionEnd{};
    Clock::time_point received=Clock::now();
    std::chrono::system_clock::time_point received_wall=std::chrono::system_clock::now();
};
struct TradingAccountsView {
    SnapshotStatus status=SnapshotStatus::Unavailable;
    std::vector<std::string> accounts;
    std::uint64_t generation=0;
    std::optional<Clock::time_point> received_at;
};
template<class T> struct TradingSnapshot {
    SnapshotStatus status=SnapshotStatus::Unavailable;
    std::vector<T> rows;
    std::optional<Clock::time_point> completed_at,last_update;
    int error_code=0;
};
struct TradingMonitorView {
    RequestId request_id=0;
    std::uint64_t generation=0;
    std::string account;
    TradingMonitorState state=TradingMonitorState::Unavailable;
    bool restart_requires_reconnect=false;
    std::optional<Clock::time_point> started_at,last_update;
    std::optional<std::chrono::system_clock::time_point> started_wall,last_update_wall;
    TradingSnapshot<TradingPosition> positions;
    TradingSnapshot<TradingAccountValue> account_values;
    TradingSnapshot<TradingOpenOrder> open_orders;
    TradingSnapshot<TradingExecution> executions;
};
// Views publish only completed initial snapshots. Continuing positions and
// account values replace keyed rows after completion; the other two are snapshots.
class TradingMonitorModel {
public:
    const TradingAccountsView& accounts() const noexcept { return accounts_; }
    const TradingMonitorView& view() const noexcept { return view_; }
    void invalidate(std::uint64_t generation) { accounts_={}; accounts_.generation=generation; view_={}; view_.generation=generation; clear_staging(); }
    void accounts(const ManagedAccountsEvent& e,std::uint64_t generation) {
        accounts_={}; accounts_.generation=generation; accounts_.received_at=Clock::now();
        std::set<std::string> seen;
        bool valid=e.success && e.accounts.size()<=128;
        for(const auto& a:e.accounts) valid=valid && trading_account(a) && seen.insert(a).second;
        accounts_.status=valid?SnapshotStatus::Complete:SnapshotStatus::Failed;
        if(valid) accounts_.accounts=e.accounts;
        if((view_.state==TradingMonitorState::Pending || view_.state==TradingMonitorState::Active) &&
            (!valid || !allowed(view_.account))) fail_all(-1050);
    }
    bool allowed(const std::string& account) const {
        return accounts_.status==SnapshotStatus::Complete &&
            std::find(accounts_.accounts.begin(),accounts_.accounts.end(),account)!=accounts_.accounts.end();
    }
    void begin(RequestId id,const std::string& account,std::uint64_t generation) {
        if(!id || !allowed(account)) throw std::invalid_argument("Select a confirmed managed account");
        view_={}; clear_staging(); view_.request_id=id; view_.generation=generation; view_.account=account;
        view_.state=TradingMonitorState::Pending; view_.restart_requires_reconnect=true; view_.started_at=Clock::now(); view_.started_wall=std::chrono::system_clock::now();
        view_.positions.status=view_.account_values.status=view_.open_orders.status=view_.executions.status=SnapshotStatus::Pending;
    }
    void stop() {
        const auto id=view_.request_id; const auto generation=view_.generation; const auto account=view_.account;
        view_={}; clear_staging(); view_.request_id=id; view_.generation=generation; view_.account=account;
        view_.state=TradingMonitorState::Stopped; view_.restart_requires_reconnect=id!=0;
    }
    void apply(const TradingMonitorEvent& e) {
        if(!e.request_id || e.request_id!=view_.request_id ||
            (view_.state!=TradingMonitorState::Pending && view_.state!=TradingMonitorState::Active)) return;
        if(const auto* end=std::get_if<TradingSectionEnd>(&e.value)) {
            if(!end->success) { fail_all(end->error_code); return; }
            switch(e.section) {
            case TradingSection::Positions: complete(view_.positions,positions_,e); break;
            case TradingSection::AccountValues:
                if(values_.empty()) { fail_all(-1051); return; }
                complete(view_.account_values,values_,e); break;
            case TradingSection::OpenOrders: complete(view_.open_orders,orders_,e); break;
            case TradingSection::Executions: complete(view_.executions,executions_,e); break;
            }
        } else if(const auto* p=std::get_if<TradingPosition>(&e.value)) {
            if(e.section!=TradingSection::Positions || !p->valid()) { fail_all(-1052); return; }
            update(view_.positions,positions_,std::make_pair(p->model_code,p->contract.con_id),*p,e);
        } else if(const auto* p=std::get_if<TradingAccountValue>(&e.value)) {
            if(e.section!=TradingSection::AccountValues || !p->valid()) { fail_all(-1052); return; }
            update(view_.account_values,values_,std::make_pair(p->tag,p->currency),*p,e);
        } else if(const auto* p=std::get_if<TradingOpenOrder>(&e.value)) {
            if(e.section!=TradingSection::OpenOrders || !p->valid()) { fail_all(-1052); return; }
            if(view_.open_orders.status==SnapshotStatus::Pending) update(view_.open_orders,orders_,
                std::make_tuple(p->perm_id,p->perm_id?0:p->client_id,p->perm_id?0:p->order_id),*p,e);
        } else if(const auto* p=std::get_if<TradingExecution>(&e.value)) {
            if(e.section!=TradingSection::Executions || !p->valid()) { fail_all(-1052); return; }
            if(view_.executions.status==SnapshotStatus::Pending) {
                const auto found=executions_.find(p->exec_id);
                if(found!=executions_.end()) {
                    const auto& old=found->second;
                    if(old.contract.con_id!=p->contract.con_id || old.quantity!=p->quantity || old.price!=p->price || old.order_id!=p->order_id || old.client_id!=p->client_id || old.perm_id!=p->perm_id || old.side!=p->side || old.time!=p->time) { fail_all(-1053); return; }
                } else update(view_.executions,executions_,p->exec_id,*p,e);
            }
        }
        if(view_.state==TradingMonitorState::Failed) return;
        view_.last_update=e.received; view_.last_update_wall=e.received_wall;
        if(view_.positions.status==SnapshotStatus::Complete && view_.account_values.status==SnapshotStatus::Complete &&
            view_.open_orders.status==SnapshotStatus::Complete && view_.executions.status==SnapshotStatus::Complete)
            view_.state=TradingMonitorState::Active;
    }
private:
    TradingAccountsView accounts_;
    TradingMonitorView view_;
    std::map<std::pair<std::string,ContractId>,TradingPosition> positions_;
    std::map<std::pair<std::string,std::string>,TradingAccountValue> values_;
    std::map<std::tuple<std::int64_t,int,int>,TradingOpenOrder> orders_;
    std::map<std::string,TradingExecution> executions_;
    void clear_staging() { positions_.clear(); values_.clear(); orders_.clear(); executions_.clear(); }
    template<class T> static void failed(TradingSnapshot<T>& v,int code) { v={}; v.status=SnapshotStatus::Failed; v.error_code=code; }
    void fail_all(int code) { view_.state=TradingMonitorState::Failed; failed(view_.positions,code); failed(view_.account_values,code); failed(view_.open_orders,code); failed(view_.executions,code); clear_staging(); }
    template<class T,class Map> static void publish(TradingSnapshot<T>& v,const Map& rows) { v.rows.clear(); for(const auto& row:rows) v.rows.push_back(row.second); }
    template<class T,class Map> static void complete(TradingSnapshot<T>& v,const Map& rows,const TradingMonitorEvent& e) {
        if(v.status!=SnapshotStatus::Pending) return;
        publish(v,rows); v.status=SnapshotStatus::Complete; v.completed_at=e.received; v.last_update=e.received;
    }
    template<class T,class Map,class Key> void update(TradingSnapshot<T>& v,Map& rows,const Key& key,const T& value,const TradingMonitorEvent& e) {
        if(rows.find(key)==rows.end() && rows.size()>=trading_row_limit) { fail_all(-1054); return; }
        rows[key]=value; v.last_update=e.received;
        if(v.status==SnapshotStatus::Complete) publish(v,rows);
    }
};
struct TradingMonitorRequests { RequestId monitor=0,positions=0,summary=0,executions=0; };
} // namespace dts
