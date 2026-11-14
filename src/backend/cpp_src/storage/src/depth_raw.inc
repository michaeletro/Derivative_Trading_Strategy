// Private raw depth provenance. SQLite remains the authoritative commit ledger.
// Files may contain an uncommitted tail after failure/crash; never accept a
// sequence greater than depth_sessions.last_sequence as committed observation.
namespace dts::storage {
namespace {
std::string depth_raw_hex(const std::string& value) {
    constexpr char digits[]="0123456789abcdef";
    std::string out;out.reserve(value.size()*2);
    for(unsigned char c:value){out+=digits[c>>4];out+=digits[c&15];}
    return out;
}
std::string depth_raw_text(const std::string& value) {
    // Source byte fields also have exact hex copies. JSON text is byte escaped
    // for a valid document even if the provider sends non-UTF-8 metadata.
    std::string out="\"";constexpr char digits[]="0123456789abcdef";
    for(unsigned char c:value) {
        if(c=='"'||c=='\\'){out+='\\';out+=static_cast<char>(c);}
        else if(c<32||c>=127){out+="\\u00";out+=digits[c>>4];out+=digits[c&15];}
        else out+=static_cast<char>(c);
    }
    return out+'"';
}
fs::path depth_raw_path(const fs::path& root,std::int64_t run,std::int64_t session) {
    return root/"depth-raw"/("run-"+std::to_string(run))/("session-"+std::to_string(session)+".jsonl");
}
void depth_raw_validate_fd(int fd) {
    struct stat st{};
    if(::fstat(fd,&st)||!S_ISREG(st.st_mode)||st.st_uid!=::geteuid()||(st.st_mode&0077)||st.st_nlink!=1)
        throw std::runtime_error("Invalid private depth metadata file");
}
void depth_raw_write(int fd,const std::string& text) {
    std::size_t offset=0;
    while(offset<text.size()) {
        const auto n=::write(fd,text.data()+offset,text.size()-offset);
        if(n<0&&errno==EINTR)continue;
        if(n<=0)throw std::runtime_error("Raw depth metadata write failed");
        offset+=static_cast<std::size_t>(n);
    }
}
void depth_raw_begin(const fs::path& root,std::int64_t run,std::int64_t session,const std::string& source,RequestId id,const DepthSpec& spec,int archive_schema) {
    const auto path=depth_raw_path(root,run,session);
    private_directory(root/"depth-raw");private_directory(path.parent_path());
    const int fd=::open(path.c_str(),O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
    if(fd<0)throw std::runtime_error("Cannot create raw depth metadata file");
    try {
        depth_raw_validate_fd(fd);
        std::ostringstream out;out.imbue(std::locale::classic());
        out<<"{\"record_type\":\"header\",\"schema_version\":1,\"archive_schema_version\":"<<archive_schema
           <<",\"session_id\":\""<<session<<"\",\"run_id\":\""<<run<<"\",\"native_request_id\":\""<<id<<"\""
           <<",\"source\":"<<depth_raw_text(source)<<",\"symbol\":"<<depth_raw_text(spec.contract.symbol)
           <<",\"contract_id\":\""<<spec.contract.id<<"\",\"currency\":"<<depth_raw_text(spec.contract.currency)
           <<",\"contract_route\":"<<depth_raw_text(spec.contract.exchange)<<",\"venue\":"<<depth_raw_text(spec.venue)
           <<",\"requested_rows\":"<<spec.rows<<",\"smart_depth\":false"
           <<",\"sequence_basis\":\"local_subscription_sequence\",\"time_basis\":\"local_callback_receipt\""
           <<",\"exchange_timestamp\":null,\"complete_exchange_book\":false"
           <<",\"commit_authority\":\"SQLite committed depth sequence excluding recorder_recovery; later raw lines are uncommitted\""
           <<",\"raw_payload_encoding\":\"hex reserialized SDK-decoded protobuf; not wire bytes; empty for legacy or lifecycle\""
           <<",\"text_encoding\":\"byte escaped JSON; *_hex fields preserve exact source bytes\""
           <<",\"recovery_markers\":\"recorder recovery markers may exist only in SQLite\"}\n";
        depth_raw_write(fd,out.str());
        if(::fsync(fd))throw std::runtime_error("Raw depth metadata flush failed");
        ::close(fd);
    } catch(...){::close(fd);throw;}
    sync_path(path.parent_path(),true);sync_path(root/"depth-raw",true);sync_path(root,true);
}
class DepthRawBatch {
    std::map<fs::path,int> files_;
public:
    ~DepthRawBatch(){for(const auto& [path,fd]:files_)::close(fd);}
    void append(const fs::path& path,const DepthEvent& e) {
        if(e.raw_payload.size()>65536)throw std::invalid_argument("Raw depth payload exceeds 64 KiB");
        small_text(e.callback_format,64);
        auto it=files_.find(path);
        if(it==files_.end()) {
            const int fd=::open(path.c_str(),O_WRONLY|O_APPEND|O_NOFOLLOW|O_CLOEXEC);
            if(fd<0)throw std::runtime_error("Raw depth metadata file unavailable");
            try{depth_raw_validate_fd(fd);}catch(...){::close(fd);throw;}
            it=files_.emplace(path,fd).first;
        }
        std::ostringstream out;out.imbue(std::locale::classic());
        out<<"{\"record_type\":\"event\",\"sequence\":\""<<e.sequence<<"\",\"request_id\":\""<<e.request_id<<"\""
           <<",\"kind\":"<<depth_raw_text(e.kind)<<",\"origin\":"<<depth_raw_text(e.origin)
           <<",\"received_unix_us\":\""<<e.received.unix_us<<"\",\"received_monotonic_ns\":\""<<e.received.monotonic_ns<<"\""
           <<",\"operation\":"<<e.operation<<",\"side\":"<<e.side<<",\"position\":"<<e.position
           <<",\"raw_price_repr\":";
        if(std::isnan(e.price))out<<"\"nan\"";
        else if(!std::isfinite(e.price))out<<(e.price<0?"\"-inf\"":"\"inf\"");
        else out<<'"'<<std::setprecision(std::numeric_limits<double>::max_digits10)<<e.price<<'"';
        out<<",\"size\":"<<depth_raw_text(e.size)<<",\"size_hex\":"<<depth_raw_text(depth_raw_hex(e.size))
           <<",\"market_maker\":"<<depth_raw_text(e.market_maker)<<",\"market_maker_hex\":"<<depth_raw_text(depth_raw_hex(e.market_maker))
           <<",\"smart_depth\":"<<(e.smart_depth?"true":"false")<<",\"code\":"<<e.code
           <<",\"callback_format\":"<<depth_raw_text(e.callback_format)<<",\"raw_payload_hex\":"<<depth_raw_text(depth_raw_hex(e.raw_payload))<<"}\n";
        depth_raw_write(it->second,out.str());
    }
    void flush(){for(const auto& [path,fd]:files_)if(::fsync(fd))throw std::runtime_error("Raw depth metadata flush failed");}
};
}
} // namespace dts::storage
