#include "browser_auth.hpp"
#include <atomic>
#include <iostream>
#include <thread>
#include <vector>
using namespace dts::local_auth;
void check(bool good, const char* label) { if (!good) throw std::runtime_error(label); }
int main() {
    const std::string host="127.0.0.1:8081", other="127.0.0.1:8082";
    const auto now=Clock::now();
    Sessions sessions;
    const auto code=sessions.issue(host,now);
    check(secret_shape(code),"OS random ticket shape");
    check(!sessions.valid(code,host,now),"ticket is not session");
    check(sessions.exchange(code,other,"",now).empty(),"ticket host bound");
    const auto session=sessions.exchange(code,host,"",now);
    check(secret_shape(session) && session!=code,"session is freshly generated");
    check(sessions.exchange(code,host,"",now).empty(),"ticket one use");
    check(sessions.valid(session,host,now) && !sessions.valid(session,other,now),"session host bound");
    check(sessions.valid(session,host,now+std::chrono::hours(11)),"session retained");
    check(!sessions.valid(session,host,now+std::chrono::hours(12)),"absolute session expiry");
    auto stale=sessions.issue(host,now);
    check(sessions.exchange(stale,host,"",now+std::chrono::seconds(60)).empty(),"ticket expires");
    const auto concurrent=sessions.issue(host,now); std::atomic<int> successes{0};
    std::vector<std::thread> threads;
    for(int i=0;i<8;++i) threads.emplace_back([&]{if(!sessions.exchange(concurrent,host,"",now).empty())++successes;});
    for(auto& thread:threads)thread.join();
    check(successes==1,"atomic ticket consumption");
    auto old=sessions.exchange(sessions.issue(host,now),host,"",now);
    auto replacement=sessions.exchange(sessions.issue(host,now),host,old,now);
    check(!sessions.valid(old,host,now) && sessions.valid(replacement,host,now),"rotation invalidates prior session");
    sessions.revoke(replacement,other);check(sessions.valid(replacement,host,now),"wrong-host revocation refused");
    sessions.revoke(replacement,host);check(!sessions.valid(replacement,host,now),"logout revokes");
    Sessions restarted;check(!restarted.valid(old,host,now),"restart does not restore sessions");
    Sessions bounded;
    for(int i=0;i<8;++i)bounded.issue(host,now);
    bool full=false;try{bounded.issue(host,now);}catch(const std::length_error&){full=true;}
    check(full,"ticket queue bound");bounded.issue(host,now+std::chrono::seconds(61));
    const auto value=random_secret();
    const auto header=set_cookie(value,8081);
    check(header.find("HttpOnly")!=std::string::npos && header.find("SameSite=Strict")!=std::string::npos,"cookie attributes");
    check(header.find("Domain=")==std::string::npos,"host-only cookie");
    check(cookie_value("other=x; "+cookie_name(8081)+"="+value,8081)==value,"cookie parsing");
    check(cookie_value(cookie_name(8081)+"="+value+"; "+cookie_name(8081)+"="+value,8081).empty(),"duplicate cookie rejected");
    check(cookie_value(header,8082).empty() && cookie_value(std::string(8193,'a'),8081).empty(),"cookie bounds");
    check(equal_secret(value,value) && !equal_secret(value,value+"a"),"secret comparison");
    std::cout<<"Local session expiry, bounds, host binding, one-use concurrency and cookie checks passed.\n";
}
