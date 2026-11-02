# [Engine] phase 1 in steps: the small plan

This file breaks the big step of the series down. One step at a time, each one compiles, its tests
pass and its commit stands on its own. The plan itself is untouched: what phase 1 is, why this
order, and the data model are in
[CONVERSATION_DISK_STORE_PHASE1.md](CONVERSATION_DISK_STORE_PHASE1.md); the measurements it answers
are in [CONVERSATION_DISK_STORE_MEASUREMENTS.md](CONVERSATION_DISK_STORE_MEASUREMENTS.md); what
phases 2 and 3 add are in
[CONVERSATION_DISK_STORE_PHASE2.md](CONVERSATION_DISK_STORE_PHASE2.md) and
[CONVERSATION_DISK_STORE_PHASE3.md](CONVERSATION_DISK_STORE_PHASE3.md).

Decisions already agreed (2026-11-02):

1. **the golden test of the gate is a GPU test.** It lives in `conversation_snapshot_test.cpp` and
   uses the real captures (`conversation_kv_save`), because a CPU-only test would compare the same
   layout formulas it computes with - a tautology. The CPU-only disk test stays for the store's own
   behaviour (the planned list in phase 1, tests 2-11).
2. **the indexer's spare pooled row goes into the state block.** A `kv` block holds the completed
   rows of its page-aligned range only. The spare row of a capture at the state's own position `P`
   (which is usually not page-aligned) is not the same bytes at any page boundary of the range; the
   state block holds it, byte-identical to a capture at `P`, and a restore takes it from there.
3. **v1 files of a persistent folder stay.** v1 passes no v2 magic check and is left in place until
   removed by hand. The store does not touch what it did not write, so v2 files are recognisable by
   magic and name without deleting anything of a v1 entry. Interrupted-write temporary files (`.tmp`)
   and a `.bin` no v1 or v2 index names are still removed - they are never restorable.

## The steps

| # | step | files | what it buys alone |
|---|---|---|---|
| 1 | **the gate: the golden test of page-aligned K/V** | `src/core/conversation_snapshot_test.cpp` | proof (or refutation) that a page-aligned range of buffer bytes read from a block equals the same range of a full write, for every format the engine produces. Nothing else of phase 1 may be written before this passes |
| 2 | **container v2**: the `.bin` header, the v2 `.idx` writer/reader, `Entry` with its block table | `conversation_disk.{hpp,cpp}`, `conversation_file.{hpp,cpp}` | the container's format is fixed; phases 2 and 3 add to it, never rewrite it |
| 3 | **the write path**: find the entry to append to (or start one), `kv` blocks by page-aligned pieces, `state` blocks, publish the index, remove the entries the new image supersedes | `conversation_disk.cpp` | append instead of rewrite, the largest single win of the plan |
| 4 | **the read path**: `take()` keeps the entry, reads only the blocks it needs, verifies hashes and the chain, falls back to the previous state block on damage | `conversation_disk.cpp` | reads no longer delete, and one bad block costs a block |
| 5 | **budget and start-up**: eviction of whole conversations by `last_used_s`, newest scan rules | `conversation_disk.{hpp,cpp}` | the budget matches real use, not write order |
| 6 | **`used` and `pinned` in the index** | `conversation_file.{hpp,cpp}`, `conversation_disk.cpp` | the LRU stamp and the pin survive a restart; the slot-save format stays v1 and byte-identical |
| 7 | **`Match::restore_tokens` in the caller** | `src/program/generate.cpp` | the "read the file or read the tokens" estimate no longer claims a longer prefix than the file can give |
| 8 | **the tests of the plan, 2-11** | `conversation_disk_test.cpp` | append, keep-on-read, budget by use, torn tail, one bad block, `used`/`pinned` across a restart, append across runs, history rewrite, v1 left in place |
| 9 | **the docs**: the tier's section in [DETAILS.md](DETAILS.md), per-block log lines | `docs/DETAILS.md`, logs | the log can answer the "new blocks versus a rewrite" question the validation needs |

Steps 2 and 3 may be written together in one branch (the store cannot be exercised without the
writer), but they land as separate commits. Step 8's tests 8-11 need step 3's supersede rules;
tests 2-7 run on what steps 3 and 4 give.

## Where each step stands

2026-11-02. **Steps 1 and 2 are done and green**; step 1 touched only the gate's test, step 2 only the
tier's own files. The engine's behaviour is unchanged: no entry is written in v2 yet, and the v1 write
and read paths still run.

**Step 1, the gate: passes.** Measured on this PC (RTX 4080, sm_89, CUDA 13.4, MSVC 14.44.35207,
build folder `build-c13-gate`, Release):

- `conversation_snapshot_test.exe`: 4865 checks, exit 0, and 100 consecutive runs with no failure.
- `ctest -C Release -R conversation_snapshot_test`: all three entries pass
  (`conversation_snapshot_test` 1.7 s, `_ckpt_async` 4.8 s, `_ckpt_vram_slots` 4.7 s).
- The other conversation tests are unaffected: `ctest -R "conversation_|session_save"` is 11/11.

Location: `src/core/conversation_snapshot_test.cpp`, function `golden_pagesafe()`, called first thing
from `main()`. What it does: for every `{fmt, mode}` of the fixture matrix
(`kKvF16/kKvInt8/kKvQ4/3` x `{0,1,2}`, hybrid only in mode 0), plus the Hadamard-rotated INT8
(#293) and the non-index (draft) extent, it captures at three page-aligned flush extents
(`page, 5*page, 9*page` with `page = qsa_real_shapes().page_size = 4`), then grows the state past
`12*page` (`fill_after(177, 12*page)`, which rewrites everything at or past the last flush's cells),
captures at `20*page`, and requires every earlier flush's bytes below its extent - and every
completed indexer row (`(b/idx_block)*idx_key_dim*4` bytes) - to equal the full write's bytes range by
range. The moving spare row is deliberately not compared (decision 2: it goes into the state block). It
also pins `sizes[0] == b*n_head_kv*per` against the engine's own `conversation_kv_part_sizes`, so the
store's layout math is checked against the engine's. **The answer is yes**: a page-aligned range of
buffer bytes read from a block equals the same range of a full write, for every format the engine
produces. Phase 1 may go on.

Two things the gate needed beyond the version written on the first pass, both of them the gate's own:

1. **The ring state is the drafter and carries no indexer history.** `layer.cpp`'s `kv_plan` gives
   `pooled_rows = 2` to a mode-2 state ("the drafter has no indexer"), and `valid()` refuses a capture
   with `index = true` there. That refusal is correct, so the gate tests the ring's K/V with
   `index = false`, as every real capture of a drafter does. (Measured: without this the gate stopped on
   its first fixture with "invalid K/V extent".)
2. **The fixture's `fill()` needed a `cudaDeviceSynchronize()`.** `Fixture::fill` writes the device
   pools with `cudaMemcpy` (legacy stream) and the capture copies them on `copy_nonblocking`'s private
   `cudaStreamNonBlocking` stream, which has no ordering with the legacy stream. Without the sync the
   capture sometimes read a pool before the fill had landed, and the gate failed at random - in a
   capture that held zeros (a fresh `cudaMalloc` buffer) or the previous fill's bytes (a reused one).
   Measured before: 1 failure in 40 runs (also 2 in 60 and 12 in 40 during the investigation). Measured
   after: 0 in 100. This is the documented contract of the copy helper ("the device was synchronized
   before the copy"), which `full_session`'s own fill lambda already honours.

**Debugging notes, so the next person does not repeat them** (all of it measured, none of it shipped):
the failing image disagreed with the source *at the moment of capture* (a second `ConversationKv` taken
from the same state had the same bytes as the first, both wrong), and re-reading the source later gave
the correct bytes - i.e. the source changed between the fill and the capture, not the copy. In isolation
both the copy path (200 000 device-to-pageable-host copies of 512/1024/1280/2560/16384 bytes over the
same private stream) and the capture path (5 000 `conversation_kv_save` calls compared part by part)
are clean, which is what pointed at the fixture and not at the engine. Do not "fix" this by putting a
device sync inside `conversation_kv_save` or `copy_nonblocking`: the sync belongs to the caller that
writes the pools.

**Build recipe that works on this PC (use it to resume):**

```sh
VS="C:/Program Files (x86)/Microsoft Visual Studio/2022/BT2022"
C="$VS/Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"

"$C" -S C:/Projects/Strata-fork -B C:/Projects/Strata-fork/build-c13-gate -G "Visual Studio 17 2022" -A x64 -DSTRATA_ENABLE_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=native -DSTRATA_BUILD_CONVERSATION_TESTS=ON
"$C" --build C:/Projects/Strata-fork/build-c13-gate --config Release --target conversation_snapshot_test
```

The conversation tests sit behind `STRATA_ENABLE_CUDA` (the block that holds them starts at
`CMakeLists.txt:423`), so `-DSTRATA_ENABLE_CUDA=ON` is not optional for this test: with CUDA off the
target is not generated at all and `--target conversation_snapshot_test` fails with MSB1009.
`build-c13-win` is a mixed cache (Ninja generator with the Visual Studio compiler) and must be deleted,
not reused; configure of `build-c13-gate` takes ~80 s and its llamacpp `FetchContent` populate works.

**Clean-up notice:** one cmake run of the previous session with a wrong working directory created then
removed a stray `C:/Projects/build-c13-win`; `build-c13-gate` is the only build folder phase 1 uses.

**Decisions that shape the next steps (agreed with the user):** see the three decisions at the
top of this file (GPU golden test; spare pooled row into the state block; v1 files of a persistent
folder stay, nothing of a v1 entry is deleted).

**Step 2, container v2: done, and the format is fixed.**

What landed (measured on this PC, the build recipe below, Release):

- `include/strata/core/conversation_disk.hpp`: `ConversationDiskBinHeader` with
  `conversation_disk_bin_header_write`/`_read` (magic `"STRACNV2"`, version 2, 64 bytes, model, config,
  entry id, stage); `ConversationDiskBlock` (offset, size, `hash_lo`/`hash_hi`, kind 1 = `kv` / 2 =
  `state`, `tokens_start`/`tokens_end`, `prefix_digest`, and `used`/`pinned` for a state block);
  `ConversationDiskStage` (one `.bin` and its blocks); `ConversationDiskEntry` (the block table plus the
  keys `best()` matches, the geometry and layer range, `tokens`, `last_used_s`, and `files` for the v1
  read path until step 4); `conversation_disk_index_write`/`_read` for v2 magic `"STRCIDX2"`.
  `Match` gains `restore_tokens` (assigned `tokens` by `best()` today; step 4 lowers it for a v2 entry).
- `src/core/conversation_disk.cpp`: the index's v2 writer (temp file + rename, blocks first) and reader
  (identity, then shape: a block kind of 1 or 2, a non-empty block, offsets that start after the header
  and only grow, a `state` block whose start equals its end, a `kv` block whose end is past its start,
  a `pinned` byte of 0 or 1, `tokens` equal to the deepest `state` block's position), and the v2 branch
  of the start-up scan (`read_index` recognises v2 by magic and resolves the stage names against the
  folder; `remove_locked` and `scan_folder` delete and keep a v2 entry's `.bin` files through
  `files_of()`). The v1 index writer and reader are unchanged.
- `src/core/conversation_disk_test.cpp`: a "container v2" block that round-trips the `.bin` header and a
  two-stage index (every field, block record, `used`/`pinned`, key), and refuses a short header, another
  magic or version, another identity, a truncated index, an unknown block kind, a state block with a
  range, an out-of-order block, an empty block, `tokens` that is not the deepest state, and an entry
  without a stage.

`conversation_file.{hpp,cpp}` are untouched: the block payloads reuse the encodings that already exist
there (`put_checkpoint`/`get_checkpoint`, `SessionHasher` for `hash_lo`/`hash_hi`), and lifting `used`
out of `put_checkpoint` is step 6's change, not this one.

Measured:

- `conversation_disk_test`: 86 checks, exit 0 (17 of them the container v2 block above).
- `ctest -C Release -R "conversation_|session_save"`: 10 of 11 pass. The one failure,
  `conversation_snapshot_test_ckpt_vram_slots` ("six more restores allocate nothing on the card"), is a
  GPU test that does not include `conversation_disk.hpp`; it passed on three reruns of the same entry,
  and the same three entries passed on step 1's run.
- the engine: `--target strata` built, exit 0 (warnings from the tree's own templates, none from the
  touched files).

**Next: step 3, the write path** - find the entry to append to (or start one), `kv` blocks by
page-aligned pieces, `state` blocks, publish the index, remove the entries the new image supersedes.
Steps 2 and 3 may share a branch but land as separate commits (the store cannot be exercised without
the writer).

## What each step must not touch

- the session files of the HTTP API (`/slots/0?action=save|restore`) - keep v1 byte-identical,
  every phase;
- the other backends - the tier is not run on HIP, SYCL or Metal ([DETAILS.md](DETAILS.md));
- the default path of a conversation without `--conversation-disk-dir` - byte-identical, as the
  plan requires.
