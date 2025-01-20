#ifndef CUSTOM_HASH_H
#define CUSTOM_HASH_H

#include <memory>
#include <functional>

namespace std {
    template <>
    struct hash<std::weak_ptr<void>> {
        std::size_t operator()(const std::weak_ptr<void>& wp) const noexcept {
            auto sp = wp.lock();
            return sp ? std::hash<void*>()(sp.get()) : 0;
        }
    };

    template <>
    struct equal_to<std::weak_ptr<void>> {
        bool operator()(const std::weak_ptr<void>& lhs, const std::weak_ptr<void>& rhs) const noexcept {
            return !lhs.owner_before(rhs) && !rhs.owner_before(lhs);
        }
    };
}

#endif // CUSTOM_HASH_H