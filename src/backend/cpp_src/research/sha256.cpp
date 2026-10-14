#include <dts/replay.hpp>
#include <array>
#include <cstdint>
#include <iomanip>
#include <sstream>
#include <algorithm>
#include <cstring>
#include <limits>
#include <stdexcept>

namespace dts::research {
namespace {
std::uint32_t rotate(std::uint32_t x, unsigned n) { return (x >> n) | (x << (32 - n)); }
constexpr std::array<std::uint32_t, 64> constants{
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
}
void Sha256::block(const std::uint8_t* bytes) {
        auto& h = state_;
        std::array<std::uint32_t,64> w{};
        for (std::size_t i=0; i<16; ++i) for (std::size_t j=0; j<4; ++j) w[i]=(w[i]<<8)|bytes[4*i+j];
        for (std::size_t i=16; i<64; ++i) {
            const auto a=w[i-15], b=w[i-2];
            w[i]=w[i-16]+(rotate(a,7)^rotate(a,18)^(a>>3))+w[i-7]+(rotate(b,17)^rotate(b,19)^(b>>10));
        }
        auto a=h[0],b=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],v=h[7];
        for (std::size_t i=0; i<64; ++i) {
            const auto t1=v+(rotate(e,6)^rotate(e,11)^rotate(e,25))+((e&f)^(~e&g))+constants[i]+w[i];
            const auto t2=(rotate(a,2)^rotate(a,13)^rotate(a,22))+((a&b)^(a&c)^(b&c));
            v=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;
        }
        h[0]+=a;h[1]+=b;h[2]+=c;h[3]+=d;h[4]+=e;h[5]+=f;h[6]+=g;h[7]+=v;
}
void Sha256::update(const char* bytes, std::size_t count) {
    if (count > std::numeric_limits<std::uint64_t>::max() / 8 - bytes_)
        throw std::length_error("SHA256 input exceeds its length bound");
    bytes_ += count;
    if (pending_size_) {
        const auto take = std::min(count, pending_.size() - pending_size_);
        std::memcpy(pending_.data() + pending_size_, bytes, take);
        pending_size_ += take; bytes += take; count -= take;
        if (pending_size_ == pending_.size()) { block(pending_.data()); pending_size_ = 0; }
    }
    while (count >= 64) { block(reinterpret_cast<const std::uint8_t*>(bytes)); bytes += 64; count -= 64; }
    if (count) { std::memcpy(pending_.data(), bytes, count); pending_size_ = count; }
}
std::string Sha256::finish() const {
    auto copy = *this;
    const std::uint64_t bits = bytes_ * 8;
    copy.pending_[copy.pending_size_++] = 0x80;
    if (copy.pending_size_ > 56) {
        std::fill(copy.pending_.begin() + copy.pending_size_, copy.pending_.end(), 0);
        copy.block(copy.pending_.data()); copy.pending_size_ = 0;
    }
    std::fill(copy.pending_.begin() + copy.pending_size_, copy.pending_.begin() + 56, 0);
    for (int i = 0; i < 8; ++i) copy.pending_[56 + i] = static_cast<std::uint8_t>(bits >> (56 - 8 * i));
    copy.block(copy.pending_.data());
    std::ostringstream out;out.imbue(std::locale::classic());out<<std::hex<<std::setfill('0');
    for (auto part:copy.state_) out<<std::setw(8)<<part;
    return out.str();
}
std::string sha256(const std::string& text) { Sha256 hash; hash.update(text); return hash.finish(); }
} // namespace dts::research
