/* Compatibility declarations for an older SDK on macOS >= 14.4.
 * Signatures: apple-oss-distributions/libplatform/include/os/os_sync_wait_on_address.h
 * The OS implements synchronization; this header only resolves the public API.
 */
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <os/clock.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>

#define OS_SYNC_WAIT_ON_ADDRESS_NONE 0
#define OS_SYNC_WAKE_BY_ADDRESS_NONE 0
static inline void *neo_sync_symbol(const char *name) {
    void *p = dlsym(RTLD_DEFAULT, name);
    if (!p) { fprintf(stderr, "Required macOS 14.4+ API missing: %s\n", name); abort(); }
    return p;
}
static inline int neo_wait(void *a, uint64_t v, size_t s, uint32_t f) {
    typedef int (*Fn)(void *, uint64_t, size_t, uint32_t);
    return ((Fn)neo_sync_symbol("os_sync_wait_on_address"))(a, v, s, f);
}
static inline int neo_wait_timeout(void *a, uint64_t v, size_t s, uint32_t f, os_clockid_t c, uint64_t n) {
    typedef int (*Fn)(void *, uint64_t, size_t, uint32_t, os_clockid_t, uint64_t);
    return ((Fn)neo_sync_symbol("os_sync_wait_on_address_with_timeout"))(a, v, s, f, c, n);
}
static inline int neo_wake_any(void *a, size_t s, uint32_t f) {
    typedef int (*Fn)(void *, size_t, uint32_t);
    return ((Fn)neo_sync_symbol("os_sync_wake_by_address_any"))(a, s, f);
}
static inline int neo_wake_all(void *a, size_t s, uint32_t f) {
    typedef int (*Fn)(void *, size_t, uint32_t);
    return ((Fn)neo_sync_symbol("os_sync_wake_by_address_all"))(a, s, f);
}
#define os_sync_wait_on_address neo_wait
#define os_sync_wait_on_address_with_timeout neo_wait_timeout
#define os_sync_wake_by_address_any neo_wake_any
#define os_sync_wake_by_address_all neo_wake_all
