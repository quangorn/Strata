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
3. **v1 files of a persistent folder are removed at start** (changed 2026-11-02, was "stay"). The store
   reads only the v2 container: a v1 index passes no v2 magic check, is refused by the start-up scan,
   and its `.bin` files go with it - the same rule the phase 1 note states ("A v1 file fails the magic
   check and is removed, which is right: it is a cache, and the worst case is one longer prompt read").
   A v1 entry is never appended to, never kept for a later read and never hand-removed. Interrupted-write
   temporary files (`.tmp`) and a `.bin` no v2 index names are removed for the same reason.
4. **testing runs on its own folder, never on the serving model's storage.** The engine on this PC serves
   with `--conversation-disk-persist on` and a full folder (measured 13 entries, 21 118 MiB); decision 3
   deletes every v1 conversation at the first start of a v1-reading build, so tests, validation runs and
   manual checks use a separate `--conversation-disk-dir` (and a separate `--conversation-cache-mib`) until
   the phase is done and the owner decides to switch the live folder. Nothing in the work touches the
   serving profile's files until then.

## The steps

| # | step | files | what it buys alone |
|---|---|---|---|
| 1 | **the gate: the golden test of page-aligned K/V** | `src/core/conversation_snapshot_test.cpp` | proof (or refutation) that a page-aligned range of buffer bytes read from a block equals the same range of a full write, for every format the engine produces. Nothing else of phase 1 may be written before this passes |
| 2 | **container v2**: the `.bin` header, the v2 `.idx` writer/reader, `Entry` with its block table | `conversation_disk.{hpp,cpp}`, `conversation_file.{hpp,cpp}` | the container's format is fixed; phases 2 and 3 add to it, never rewrite it |
| 3 | **the write path**: find the entry to append to (or start one), `kv` blocks by page-aligned pieces, `state` blocks, publish the index, remove the entries the new image supersedes | `conversation_disk.cpp` | append instead of rewrite, the largest single win of the plan |
| 4 | **the read path**: `take()` keeps the entry, reads only the blocks it needs, verifies hashes and the chain, falls back to the previous state block on damage; and the writer waits for a background park copy (`STRATA_PARK_ASYNC`) before it reads an image | `conversation_disk.{hpp,cpp}` | reads no longer delete, one bad block costs a block, and a write never reads K/V that is still landing |
| 5 | **budget and start-up**: eviction of whole conversations by `last_used_s`, newest scan rules | `conversation_disk.{hpp,cpp}` | the budget matches real use, not write order |
| 6 | **`used` and `pinned` in the index** | `conversation_file.{hpp,cpp}`, `conversation_disk.cpp` | the LRU stamp and the pin survive a restart; the slot-save format stays v1 and byte-identical |
| 7 | **`Match::restore_tokens` in the caller** | `src/program/generate.cpp` | the "read the file or read the tokens" estimate no longer claims a longer prefix than the file can give |
| 8 | **the tests of the plan, 2-11** | `conversation_disk_test.cpp` | append, keep-on-read, budget by use, torn tail, one bad block, `used`/`pinned` across a restart, append across runs, history rewrite, a v1 folder removed at start |
| 9 | **the docs**: the tier's section in [DETAILS.md](DETAILS.md), per-block log lines | `docs/DETAILS.md`, logs | the log can answer the "new blocks versus a rewrite" question the validation needs |

Steps 2 and 3 may be written together in one branch (the store cannot be exercised without the
writer), but they land as separate commits. Step 8's tests 8-11 need step 3's supersede rules;
tests 2-7 run on what steps 3 and 4 give.

## Where each step stands

2026-11-02. **Steps 1, 2, 3, 4, 5, 6 and 7 are done and green.** Step 1 touched only the gate's test; steps 2-6
the tier's own files; step 7 the serve loop's restore estimate. A park now writes the block store, a read keeps the
entry and reads only the blocks its
state needs, the budget evicts whole conversations by use and leaves a pinned one, `used` and `pinned` survive a
restart through the index, a folder of the previous build is refused and removed at start (decision 3), and the
"read the file or read the tokens" estimate counts what a read lands on rather than what the index claims. The
default path (no `--conversation-disk-dir`) is untouched, and every test run uses its own temporary folder - never
the serving model's storage (decision 4).

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

**Decisions that shape the next steps (agreed with the user):** see the four decisions at the
top of this file (GPU golden test; spare pooled row into the state block; v1 files of a persistent
folder are removed at start; testing on its own folder, never the serving model's storage).

**Step 2, container v2: done, and the format is fixed.**

What landed (measured on this PC, the build recipe below, Release):

- `include/strata/core/conversation_disk.hpp`: `ConversationDiskBinHeader` with
  `conversation_disk_bin_header_write`/`_read` (magic `"STRACNV2"`, version 2, 64 bytes, model, config,
  entry id, stage); `ConversationDiskBlock` (offset, size, `hash_lo`/`hash_hi`, kind 1 = `kv` / 2 =
  `state`, `tokens_start`/`tokens_end`, `prefix_digest`, and `used`/`pinned` for a state block);
  `ConversationDiskStage` (one `.bin` and its blocks); `ConversationDiskEntry` (the block table plus the
  keys `best()` matches, the geometry, `tokens`, `last_used_s`); `conversation_disk_index_write`/`_read`
  for v2 magic `"STRCIDX2"`.
  `Match` gains `restore_tokens` (assigned `tokens` by `best()` today; step 4 lowers it for a v2 entry).
- `src/core/conversation_disk.cpp`: the index's v2 writer (temp file + rename, blocks first) and reader
  (identity, then shape: a block kind of 1 or 2, a non-empty block, offsets that start after the header
  and only grow, a `state` block whose start equals its end, a `kv` block whose end is past its start,
  a `pinned` byte of 0 or 1, `tokens` equal to the deepest `state` block's position), and the v2 branch
  of the start-up scan (`read_index` recognises v2 by magic and resolves the stage names against the
  folder; `remove_locked` and `scan_folder` delete and keep a v2 entry's `.bin` files through
  `files_of()`).
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

**Step 3, the write path: done.** A park now appends the blocks it does not have instead of writing the
image again, and publishes a v2 index. What landed:

- `write()` (`src/core/conversation_disk.cpp`) plans the delta before touching the disk: per stage, the
  `kv` ranges the entry does not cover yet (page-aligned, at most `kBlockTokens = 2048` tokens each) and
  the states it does not hold (the image's live state and its checkpoints), with their exact byte sizes,
  so the budget can remove whole conversations first - never the entry being extended.
- the append target is the longest entry whose chain is a prefix of this image: same stages, layer
  carves and layer counts; every stored state's tokens and images equal to the image's at that length;
  every stored `prefix_digest` recomputed from the image. No target: a new entry and its own files.
  Then the entries this image supersedes (whole chains that are prefixes and shorter) are removed with
  their files after the index is published.
- the index the entry already has is rewritten in place (an entry taken in from an earlier run keeps its
  file names); a `.bin` is appended from the end of the last indexed block, its header is checked
  (model, config, stage), and a torn tail past that point is cut back when the write ends.
- block payloads: a `kv` block is, per layer of the stage, the layer's K/V header (format, heads,
  head_dim, page, indexer width, indexer row bytes) and the five parts of the range - the indexer's
  completed rows only, sliced on the page boundary, which is the indexer's block in this engine; a
  `state` block is the checkpoint's five running-state arrays. The state's ids and images are the
  index's key, `used`/`pinned` are the record's, and the moving spare indexer row is the checkpoint's
  `dead`, which is byte-identical to a capture at that position (agreed decision 2).
- `take()` reads a v2 entry back: every block verified against its 128 bits, the stages, live state and
  checkpoints rebuilt, the spare row put back from the state block. It still removes the entry after
  the read: keeping it is step 4's change, and so are reading only the blocks a request needs and the
  fallback to an earlier state when a block is damaged.
- the v1 index writer and the v1 read path are gone: every write is v2, and a folder of the previous build
  is refused and removed at start (decision 3).

Two corrections to step 2's format, which no code had written yet: the stage record carries its own
`layer_lo`/`layer_hi`/`layers` (a layer split gives each stage its own carve, which one entry-level pair
could not express), and the `.bin` header's id is not compared when appending, because the start-up scan
renumbers the entries it takes in exactly as it does for the v1 index.

The test fixture now builds K/V the way `conversation_snapshot.cpp` does (cells rounded up to the page,
one indexer row per page plus the spare row, and that spare row also as the live state's `dead`). The old
fixture's `cells = 1024` with 9 pooled rows at page 64 is not a layout the engine can produce, and the
store refuses it instead of slicing it wrongly.

Measured on this PC (same build folder, Release):

- `conversation_disk_test`: **116 checks, exit 0** (86 before; the store's own block adds 30: two
  page-aligned `kv` blocks in one append, blocks tiling `[0,4200)`, the `.bin` equal to its header and
  its blocks with a 1000-byte torn tail cut back, the append writing less than the whole K/V again, the
  read back byte for byte across two writes and two runs, and a stale shorter copy of the same
  conversation removed with its files).
- `ctest -C Release -R "conversation_|session_save"`: 11 of 11 passed in one run. In a later run
  `conversation_snapshot_test_ckpt_vram_slots` failed ("six more restores allocate nothing on the card")
  while the serving model held 15.6 GB of the card's 16 GB (`nvidia-smi`); three reruns in that state
  failed the same way, and that test does not include `conversation_disk.hpp`.
- the engine: `--target strata` built, exit 0; no warning from the touched files.

Follow-up the same day (decision 3 changed): the v1 read path is removed, so the v1 index parser, `Entry::files`,
`take()`'s v1 branch and its `holds()` check are gone. `conversation_disk_test`: **122 checks, exit 0** - the six
new ones write a well-formed v1 index (`"STRCIDX1"`, the identity, a payload file name) and its `.bin` beside a v2
entry, and require the v1 pair removed at start with the v2 entry taken in, and `1 refused` in the start line.
This is the "v1 refusal" test of the plan (test 11) arriving early.

**Step 4, the read path: done.** A read no longer deletes: the entry stays on the disk and only the blocks it needs
are read, a damaged chain falls back to an earlier state, and the writer waits for the park's background copy on its
own thread. What landed:

- `take()` (`src/core/conversation_disk.cpp`) copies the entry's table under the lock (a few hundred bytes), bumps
  the reader's `last_used_s`, and leaves the entry and its files in place. A queued image still leaves RAM for the
  caller. The reader's copy is marked with a `readers` count that the writer respects: the budget's eviction and the
  supersede rule skip an entry a read holds (the next publish removes it), so a concurrent append can never delete
  the files under a read.
- the state a read lands on is the deepest one the match can use: `state_positions()` keeps the keys (live and
  checkpoints) at or below the prefix the request matched and that *every* stage holds a `state` block for. Only the
  `kv` blocks below it and the `state` blocks at or below it are read; the last `kv` block may straddle the state's
  position, and only the cells up to `round_up(position, page)` are copied (the golden test of step 1 is what makes
  that prefix valid). The image's `live` is that state, its checkpoints are the states below it, and the moving spare
  indexer row still comes from the state's own block.
- every block read is checked twice: its 128 bits, and its chained `prefix_digest` recomputed from the index's key
  (the deepest state's ids and images). The digest chain is recomputed for the whole table but compared only for the
  blocks the read needs, so damage above the state cannot fail a shorter read.
- damage falls back: the states are tried deepest first, and the first whose chain verifies is the one returned. Each
  failed attempt logs the state and the block that failed; the hit line says where it landed
  (`from the state at N (the deeper one is damaged)`). `ConversationDiskStats::fallbacks` counts them.
- `Match::restore_tokens` is now honest: after a read fell back, the entry's in-memory `verified` position caps it,
  so `best()` claims only what a read can give (the index itself is not rewritten: the damage is the file's state,
  not the index's). `take()` also reports the tokens it actually rebuilt: the serve loop sets `parked.tokens` from the
  image it got (`generate.cpp`), so a fallback can never claim a longer prefix than the state holds.
- `ConversationDiskOptions::join` is called on the writer thread for every image of a job before its K/V is read;
  `generate.cpp` sets it to `conversation_park_join_for`. With `STRATA_PARK_ASYNC` the request thread must not wait
  for the park's background copy (that would undo the asynchronous park), and the writer is the first reader of the
  host buffers the copy fills.

Measured on this PC (same build folder, Release):

- `conversation_disk_test`: **156 checks, exit 0** (122 before; 34 new: keep-on-read twice with the same image, the
  counters, the deepest read taking every block, an earlier state reading fewer, a damaged middle `kv` block falling
  back from 4200 to 600 tokens with its two checkpoints and its state's bytes, `restore_tokens` lowered by the read
  and by a fresh tier's own read, the entry and files kept, and the join hook called once on the writer thread). The
  earlier tests now assert keep-on-read: a refused RAM preflight leaves the entry and its files too.
- `ctest -C Release -R "conversation_|session_save"`: 11 of 11 passed (`conversation_disk_test` 0.33 s; all three
  snapshot entries included).
- the engine: `--target strata` built, exit 0; the only warnings from the touched files are the tree's own `getenv`
  C4996 lines in `generate.cpp`.

**Step 5, budget and start-up: done.** The budget evicts whole conversations by use, not by write order, and the
start-up scan refuses what a read could not rebuild. What landed:

- `lru_s()` and `lru_victim()` (`src/core/conversation_disk.cpp`): an entry's use is the later of `last_used_s` (a
  read's stamp, step 4) and `written_s` (a publish's), so an entry written after a read is not old; a tie falls to the
  lower id. Both budget loops (`write()` step 3 and the scan's trim at start) take the victim from `lru_victim()`, so
  what goes is the least recently used conversation that is neither the entry being extended nor one a read holds -
  never a block and never a part of a conversation. The scan keeps `entries_` in that order.
- `check_files()`, called by `read_index()` and so only at start: an entry is taken in only when every stage holds a
  `state` block, every stage's `.bin` is at least as long as the table says (a longer file is a torn tail: kept, with
  the excess ignored until the next append), its header names this model and these settings, and it names this entry
  and this stage. The id and the stage are compared against the `.bin`'s own name (`name_ids()`), so a file of another
  entry or another stage left under an index is refused; a name the tier's shape does not match is checked by its
  header's stage alone.
- nothing else moved: `write()`'s append rules, `take()`, the index format and the session files stay steps 3's and 4's.

Measured on this PC (same build folder, Release):

- `conversation_disk_test`: **187 checks, exit 0** (156 before; 31 new: three conversations where the oldest is read
  and the second-oldest is the one a fourth evicts; a stateless index removed at start; a stage without state blocks
  removed; a `.bin` cut into its last block removed; a `.bin` whose header names another id removed; one naming
  another stage removed; a `.bin` with a 1000-byte torn tail taken in, kept and read back). The persist test that
  copies one entry's `.bin` over another's now restores the second file's own header before the read, so it still
  proves the block hashes catch foreign bytes while the header-of-another-entry case is a start-up refusal.
- `ctest -C Release -R "conversation_|session_save"`: 11 of 11 passed (`conversation_disk_test` 4.8 s: the LRU order
  is in seconds, so the budget test sleeps three times to order its writes). The usual GPU flake appeared in one run
  and passed on the next (`_ckpt_vram_slots`, with the serving model holding 15.6 GB of the card's 16 GB; that test
  does not include `conversation_disk.hpp`).
- the engine: `--target strata` built, exit 0; no warning from the touched files.

**Step 6, `used` and `pinned` in the index: done.** The LRU stamp and the shared-prefix pin belong to the index now, so
both survive a restart, and the slot-save format is byte for byte what it was. What landed:

- `put_checkpoint`/`get_checkpoint` (`src/core/conversation_file.cpp`) no longer carry `used`: the checkpoint encoder
  is the ids, the images and the five arrays, and `put_payload`/`get_payload` write and read the stamp right after
  each checkpoint - the same position in the same order, so neither the bytes nor the format's version change. The
  stamp belongs to whoever stores the checkpoint: the session payload keeps it where v1 always had it, and the disk
  tier's `state` block record carries it (container v2's `used`, step 2) with `pinned` beside it.
  `conversation_file.hpp` needed nothing: the helpers are internal to the `.cpp`, because step 3 gave the store its
  own state payload rather than reusing this encoder.
- the disk budget honours the pin (`lru_victim()` in `src/core/conversation_disk.cpp`): a conversation that holds a
  pinned `state` block is skipped while another one can go - the rule the RAM cache already applies
  (`conversation_cache.hpp`, `pinned()`), and the reason `pinned` had to survive a restart. With only pinned
  conversations left the budget goes over rather than trading the pin for room, and one line says so.

Measured on this PC (same build folder, Release):

- `conversation_disk_test`: **198 checks, exit 0** (187 before; 11 new: a stop and a start keep each checkpoint's
  `used` and its `pinned`, the start's trim evicts the unpinned conversation and keeps the pinned one, a later write
  goes over the budget rather than evicting the pin, and the line says why).
- `conversation_file_test`: **183 checks, exit 0**, the golden included: the fixed sample's file is still 17 878 535
  bytes with hash `0x70f812b350360d09` - lifting `used` out of the checkpoint encoder leaves the session file's bytes
  untouched.
- `ctest -C Release -R "conversation_|session_"`: 11 of 11 passed (all three snapshot entries in one run this time).
- the engine: `--target strata` built, exit 0; the only warnings from the touched files are the tree's own `strerror`
  C4996 lines in `conversation_file.cpp`.

**Step 7, `Match::restore_tokens` in the caller: done.** The serve loop's "read the file or read the tokens"
estimate no longer claims a prefix the file cannot give. What landed, in `src/program/generate.cpp` only:

- the estimate counts `on_disk.restore_tokens` instead of `on_disk.tokens` (`added = restore_tokens -
  reused_without_disk`), so a file of a conversation whose deepest state sits one checkpoint interval behind the
  index's key is weighed for the tokens a read lands on, not for a prefix it does not hold;
- a read that already fell back on damage (step 4 lowered the entry's `verified` position) lowers the estimate to its
  own outcome on the next request too, so the "read the file or read the tokens" line and the "not restored" line
  speak of the same number;
- `conv_disk.take()` is asked for exactly that number, so a repeat read does not re-try a state whose block is
  already known damaged; the call's `tokens` argument is diagnostics (the read still clamps it to its own deepest
  state), which is why nothing else in `take()` moved.

Measured on this PC (same build folder, Release):

- the engine: `--target strata` built, exit 0; the only warnings from `generate.cpp` are the tree's own template
  and C4996 lines.
- `ctest -C Release -R "conversation_|session_"`: 11 of 11 passed in one run (`conversation_disk_test` 4.84 s, all
  three snapshot entries included, 20.95 s total). Step 7 has no test of its own: the estimate is a branch in the
  serve loop, and step 8's tests are the store's.

**Next: step 8, the tests of the plan, 2-11** (`conversation_disk_test.cpp`): append, keep-on-read, budget by use,
torn tail, one bad block, `used`/`pinned` across a restart, append across runs, history rewrite, a v1 folder removed
at start.

## What each step must not touch

- the session files of the HTTP API (`/slots/0?action=save|restore`) - keep v1 byte-identical,
  every phase;
- the other backends - the tier is not run on HIP, SYCL or Metal ([DETAILS.md](DETAILS.md));
- the default path of a conversation without `--conversation-disk-dir` - byte-identical, as the
  plan requires.
