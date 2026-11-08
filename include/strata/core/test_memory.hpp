// include/strata/core/test_memory.hpp - a GPU test asks for the memory it is about to allocate, before it allocates it.
//
// The parity tests build the inputs and the outputs of a real prompt path on synthetic data. On a card that a model is
// serving on, that allocation is what stopped the engine: measured 2026-11-08, gemm_w16_parity took 7.6 GiB of the card
// and about 8.3 GiB of RAM while IQ2_XS was serving, and the engine's verify timed out at layer 14 ("the GPU did not
// finish within 5 s") while the test ran; gdn_rec_parity's longest prompt (T 32768 in its default run, 51820 with --long)
// builds 5.4 GiB and 7.2 GiB of the same kind of arrays.  A test that asks first and cannot fit exits 77, which ctest
// reports as a skip (SKIP_RETURN_CODE 77 is already set on these entries), so a parity test never takes the memory a
// serving model holds.
//
// test_device_enough needs only the CUDA runtime (the tests that link nothing else use it).  test_memory_enough adds the
// RAM term through available_host_bytes() from strata/core/conversation_memory.cpp, which strata_engine links: use it in
// a test whose executable links strata_engine or strata_prefill.
#pragma once

#include <cuda_runtime.h>

#include <cstdint>
#include <cstdio>

#include "strata/core/conversation_memory.hpp"

namespace strata::core {

/// `need_device` is the bytes the test is about to hold on the card at its largest step; `name` is the test's own name,
/// printed in the skip line.  Returns true when the card has it.  A card that cannot be asked (no device, an error from
/// cudaMemGetInfo) gives no skip: the test runs as it did before.  The line says what was free.
inline bool test_device_enough(uint64_t need_device, const char* name) {
    const double GiB = 1073741824.0;
    size_t free_device = 0, total_device = 0;
    if (cudaMemGetInfo(&free_device, &total_device) != cudaSuccess) return true;
    if ((uint64_t) free_device < need_device) {
        std::printf("%s: needs %.2f GiB on the card, %.2f GiB free - skipped\n", name, need_device / GiB, free_device / GiB);
        return false;
    }
    return true;
}

/// The card and the RAM.  `need_host` is the bytes the test is about to hold in RAM (0 when the test's host side is
/// small); `name` is printed in the skip line.  RAM that cannot be read gives no skip.  Links available_host_bytes().
inline bool test_memory_enough(uint64_t need_device, uint64_t need_host, const char* name) {
    if (!test_device_enough(need_device, name)) return false;
    const double GiB = 1073741824.0;
    const auto free_host = available_host_bytes();
    if (need_host && free_host && *free_host < need_host) {
        std::printf("%s: needs %.2f GiB of RAM, %.2f GiB free - skipped\n", name, need_host / GiB, *free_host / GiB);
        return false;
    }
    return true;
}

}  // namespace strata::core
