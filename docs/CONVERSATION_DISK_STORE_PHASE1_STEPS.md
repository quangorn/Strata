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

2026-11-02. **All nine steps are done and green.** Step 1 touched only the gate's test; steps 2-6
the tier's own files; step 7 the serve loop's restore estimate; step 8 the tier's tests; step 9 the tier's log and
[DETAILS.md](DETAILS.md). A park now writes the block store, a read keeps the entry and reads only the blocks its
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

**Step 8, the tests of the plan, 2-11: done.** All of the plan's CPU tests are in `conversation_disk_test.cpp`, and the
one that was missing (test 10, history rewrite) arrived with this step. What landed:

- the plan's list now has a home: the file's header says which block is which test (2 "the block store's write path",
  3 and 6 "background write"/"the read path", 4 "the budget by use", 5 and 8 "start-up", 7 "the LRU stamp and the
  pin", 9 "the block store's write path" and "supersede", 10 "history rewrite", 11 "a folder of the previous build"),
  because the blocks were written one step at a time and the numbering no longer matches their order in the file;
- **test 10, history rewrite**: a client that edited its own history sends 600 tokens that diverge from the stored
  ones at 400, so no stored chain is a prefix, the write starts a new entry, appends nothing and supersedes nothing
  (all three lines are checked), and each of the two entries has its own `.bin` and `.idx` - two entries, four files,
  no orphan. Both read back byte for byte, each on its own tokens. The old one is not dead weight: the tokens before
  the divergence are still a valid read, so it stays until the budget takes it by use - with room for two and not for
  three, reading the original first and the rewritten one second makes the original the write's victim, and its index
  and `.bin` go with it;
- **the plan's wording for test 10 was corrected**, not the code: it says "the old one is gone" after a diverged write,
  which the write path cannot do (the supersede rule removes only entries whose whole chain is a prefix, and a diverged
  entry is not one - its shorter prefix stays useful, and `best()` still finds it). The test asserts what the store
  does, and the diverged entry leaves through the budget instead;
- a stale comment on the refused-read test ("the files are gone anyway", from before step 4) now says what the test
  asserts: the entry and its files stay.

Measured on this PC (same build folder, Release):

- `conversation_disk_test`: **212 checks, exit 0** (198 before; 14 new, the history-rewrite block). It takes 5.7 s, of
  which 2.2 s are the two ordering pauses (1.1 s each).
- `ctest -C Release -R "conversation_|session_"`: 11 of 11 passed in one run, 21.30 s in all (all three snapshot
  entries included).

**Step 9, the docs and the log: done.** What landed:

- `src/core/conversation_disk.cpp`: the writer logs every block it adds - `new kv block stage 0: tokens 600-2648,
  4.6 MiB at 1418495`, `new kv block stage 0: tokens 2648-4200, 3.5 MiB at 6202807` and
  `new state block stage 0: at 4200 tokens, 0.0 MiB at 9828463` (a pinned state says so; the test images' states are
  a few KiB, hence the 0.0) - so a write's log answers "how much of this was new" block by block; the write's summary
  now separates them (`wrote 4200 tokens (appended): 9 MiB in 5 blocks (2 new kv, 8.0 MiB; 3 new state, 0.0 MiB),
  index 0.0 MiB rewritten`), and a write that found no entry to append to says `new entry: no stored conversation is a
  prefix of this one's 600 tokens`. Nothing else in the write path moved: same blocks, same order, same index.
- `docs/DETAILS.md`'s tier section describes the store as it is: `.bin` + `.idx`, blocks of at most 2,048 tokens on
  whole pages and states with `used`/`pinned`, append instead of rewrite, a shorter superseded copy removed with its
  files, keep-on-read, the 128-bit block hashes and the chain fallback, the budget by use and the pin, the v1 refusal
  and the torn tail, the estimate counting `restore_tokens`, and the per-block log lines. The paragraph's measured
  numbers are marked as the file-per-conversation version's: the block store's own comparison has not been run on
  hardware yet.
- **a write that would fill the disk is refused again.** The v1 writer used the session files' free-space preflight
  (`--session-min-free-mib`, `conversation_file.cpp:1049`) and step 3's block writer did not carry it over, so a full
  volume could take a cache write to 0 bytes free. `session_free_bytes()` (new in `conversation_file.{hpp,cpp}`, the
  same platform call the session writer makes) is asked by the writer for the blocks it is about to add, before
  anything is evicted for them; without the reserve the write is dropped with `not enough disk space (N MiB plus a
  reserve of M MiB needed, K MiB free)` and nothing is written.
- the new lines and the reserve are asserted in `conversation_disk_test.cpp` (seven new checks on top of step 8's):
  the append writes two `new kv block` lines and three `new state block` lines, its summary splits `2 new kv` from
  `3 new state` and the index's size, a rewrite logs `new entry: no stored conversation is a prefix`, and a write
  whose blocks plus the reserve do not fit the free space (`min_free_bytes = UINT64_MAX / 2`) leaves no entry, no
  file, one `write_failed` and `not enough disk space` in the log.

Measured on this PC (same build folder, Release):

- the engine: `--target strata` built, exit 0; no warning from the touched files.
- `conversation_disk_test`: **219 checks, exit 0** (step 8's 212 plus the seven above).
- `conversation_file_test`: **183 checks, exit 0**, the golden included: the fixed sample's file is still 17 878 535
  bytes with hash `0x70f812b350360d09` - the new helper does not touch the format.
- `ctest -C Release -R "conversation_|session_"`: 11 of 11 passed in one run, 21.67 s in all.

**Phase 1 is complete in code and tests. What is left is the plan's own validation on hardware**
([CONVERSATION_DISK_STORE_PHASE1.md](CONVERSATION_DISK_STORE_PHASE1.md), "Validation on hardware"): a day of agent
sessions against a same-day run without the change, the `new blocks versus a rewrite` share now readable from the log,
and start-up with a full folder. None of it is run, and nothing here claims a saving. The Windows build is done - this
PC is Windows and MSVC 14.44, and `--target strata` is green - but no serving session was measured.

**Follow-up, 2026-11-03: the store served on this PC, the writes and the append are good, and one reader rule
was wrong.** The engine of package `0.1.55.3-windows-x86_64-cuda-cuda13` (its own string `strata 0.1.55-local`,
manifest `build_id 73430364c9bf` = step 9, `BUILD.json` version `0.1.55`) served with
`--conversation-disk-dir E:\strata-conv-data --conversation-disk-mib 153600 --conversation-disk-persist on`.
The traffic was synthetic: 25 requests over 21 conversations of 24k-48k tokens, started through
`/v1/chat/completions`, so parking (a switch-time event) and the RAM-cache evictions that feed the disk were real.
Nineteen writes landed: 18 new entries and one append; the folder holds 18 entries (12 GiB). Measured:

- **writes**: 18 of them. The biggest: `wrote 48196 tokens: 865 MiB in 28 blocks (24 new kv, 414.8 MiB;
  4 new state, 450.4 MiB), index 0.6 MiB rewritten, 1 file, 1048 ms (866 MB/s)`. The per-block lines matched
  the parsed block tables block by block (`new kv block stage 0: tokens 22528-24576, 17.6 MiB at 203305408`).
- **append**: `wrote 24662 tokens (appended): 663 MiB in 1 blocks (0 new kv, 0.0 MiB; 1 new state, 112.6 MiB)`.
  The entry grew by its delta only, and the index stamp `last_used_s` of a read that had bumped it was published
  with the append.
- **the folder stayed consistent under the live writes**: 18 `.idx` and 18 `.bin`, every `.bin` named by an index,
  no `.tmp`, every `.bin` size = the 64-byte header + its blocks (no torn tail), kv blocks contiguous from token 0
  in page-aligned pieces of at most 2048 tokens, state blocks with `start == end`, `used` stamps of the checkpoints
  in the records. The `.bin` headers all read `STRACNV2` version 2 with this model and these settings.
- **reads: 5 attempts, 0 disk hits.** Every one failed the same way:
  `read of 24649 tokens: the state at 24649 could not be read (the kv blocks of a stage disagree on their layers)`,
  then the same for the state at 16384, then `read of 24649 tokens failed after N ms`. The reason is in the reader,
  not in the files: the drafter's ring layer (layer 12 of this model's 13-layer stage) carries `idx_dim = 128` and
  `pooled_rows = 0` - `layer.cpp`'s `kv_plan` gives the drafter no indexer, and `valid()` refuses a capture with
  `index = true` there - so every kv block of it writes `row = 0`, which is what the writer's own rule does
  (`plan_of`, line 340, applies `row == idx_dim * 4` only to a layer that has rows). The reader compared
  `(size_t) kv.idx_dim * sizeof(float) != row` for every layer and demanded 512 bytes from a layer that has none.
  The blocks themselves agree byte for byte: parsing the `.bin` of the failed entries showed all 13 layers' headers
  identical block by block, with `idx_dim=128, row=512` on the 12 indexed layers and `idx_dim=128, row=0` on the ring.

The reader's rule is now the writer's rule: a block that carries rows must carry `idx_dim * 4` bytes, and a layer
that established no rows must keep `row = 0`. A block that tries to carry rows for a row-0 layer was already refused
by the copy path (`rec.tokens_start / page + rows > kv.pooled_rows`). The test added is the store's own: a stage of
three K/V layers, the last one with an `idx_dim` and no indexer rows, written as two appends and read back. Measured
on this PC (same build folder, Release): `conversation_disk_test`: **229 checks, exit 0** (219 before; 10 new). The
rest of the suite is unchanged: `conversation_file_test` 183 checks with the golden sample (17 878 535 bytes, hash
`0x70f812b350360d09`), `conversation_snapshot_test` 4865, `conversation_validation_test` 972,
`session_save_reclaim_test` 119, `conversation_cache_test` 4854. The engine: `--target strata` built, exit 0.

**The live disk read now passes on this PC** (`0.1.55.4-windows-x86_64-cuda-cuda13`, `build_id 419434549fd8` = the
fix's commit, its own string `strata 0.1.55-local`). The start took the folder in whole:
`23 conversations from earlier runs (17592 MiB), 0 refused (a v1 index, another model, engine version or settings,
a missing state block, a file the table outgrew)`. Reads: `disk hit: 167790 tokens, 2232 MiB in 1 file, 2774 ms
(844 MB/s)` and `disk hit: 24649 tokens, 454 MiB in 1 file, 692 ms (688 MB/s)`, and the prompt line names where the
read landed - `prompt 176559 tokens = 167790 reused + 8769 read in 8099 ms (1082.7 tok/s), ... reused from the disk
in 3.0 s`. No read failures, no fallbacks. One read was refused by the tier's own economics and said so:
`not read: its 1058 more tokens read in ~0.6 s, its file (2232 MiB) in ~2.8 s`. The plan's own comparison (a day of
agent sessions against a run without the change) is still not run, and nothing here claims a saving.

**A serve-loop finding from the same traffic, not of the disk tier**: a request whose assistant turn carries a `null`
content makes the restore fail and the engine ends with code 1 (`generate.cpp:14823`:
`std::printf("ERR restoring a conversation checkpoint failed"); return 1;`); the server turns it into
`400 invalid request: restoring a conversation checkpoint failed` and starts the engine again on the next request, so
a conversation is lost and the process reloads (~15 minutes). The disk store survived that exit: the restart took all
23 entries in with `0 refused`. A `null`-content request should be answered with a 400 before the restore is
attempted. Read from the code, not investigated further - marked here as unconfirmed reading of the source.

**2026-11-03, the crash under a large conversation, found and fixed.** The engine ended 4 times in 13 minutes
(`engine started:` lines at 23:42:54, 23:43:16, 23:44:05, 23:46:00), each time on the same traffic: the server logged
`the engine reported an error: restoring a conversation checkpoint failed`, then `the engine stopped unexpectedly
(exit code 1)`. The engine log before each restart ends the same way: `conversation disk: disk hit: 205749 tokens,
2673 MiB in 1 file` , `conversation cache: restored 205749 tokens (checkpoint)`, and immediately
`conversation disk: end: 28 conversations kept on the disk for the next start` - the request restored the image and
the process ended, before any prompt line. The conversation the user moved to `E:\_bad` is the entry the reads land
on: its index has `tokens 205923` (the live key), `checkpoints 8`, and the prompt shares only up to the checkpoint at
205749.

The path is in `generate.cpp`, not in the store. `read_entry_image` returns the state a read lands on as the image's
`live` and the states below it as its checkpoints (`conversation_disk.cpp:871-872`). The serve loop took the match's
`live` flag - "the prompt matched the entry's deepest key to its end" - and used it as `from_live` (line 14412). When
the prompt diverges inside the entry's live key, the match is a checkpoint, `from_live` is false, and the mount
(line 14789) searches `checks` for a checkpoint whose length is `resume`; in a disk image the landing key lives in
`live`, not in the checkpoints, so the search finds none: `std::printf("ERR restoring a conversation checkpoint
failed"); return 1;` (line 14823). The image itself restored fine - `conversation_snapshot_restore` writes the
landing state as the session's live state, which is what the request needs. The fix sets `parked.live = true` after a
disk take: a disk read always lands on the state it gives back as `live`, so the session resumes as live from it.
The store test now states the invariant the launch leans on: the read of the state at 600 tokens gives
`part.live.ids.size() == 600`, no checkpoint of 600 tokens in `part.checkpoints`. `conversation_disk_test`:
**230 checks passed** (229 before, 1 added). The rest of the offline suite: `conversation_file_test` 183,
`conversation_snapshot_test` 4865, `conversation_validation_test` 972, `conversation_cache_test` 4854,
`session_save_reclaim` 119. The engine (`--target strata`) built, exit 0.

**A packaging finding measured on this PC, and the restarted engine.** `packaging/build_engine_win_local.py` with the
`package` action packages the last build, as its docstring says; it does not compile. The first two packages of the
fix (`0.1.55.5-...`, manifest `a7e01099…`, and `0.1.55.6-...`, manifest `ec3d7bf3…`, `build_id af4ea7e4585e` = the
fix's commit) were built this way, so their `strata.exe` is the previous engine: the three exes `0.1.55.4`,
`0.1.55.5`, `0.1.55.6` are byte-identical, md5 `2d347b43a8952e6f88cc773884c585da`. The manifest stamps the git
commit, not the compiled code, so a `build_id` of the fix's commit does not say the fix is in the binary. The
restart on `0.1.55.6` kept crashing with the same line (`the engine reported an error: restoring a conversation
checkpoint failed`, `the engine stopped unexpectedly (exit code 1)`, engine starts at 00:13:05, 00:13:50, 00:14:52),
and each of those runs logged `restored N tokens (checkpoint)` for a disk read - the old code's line. The user moved
the two conversation files that drew these reads out of the folder, and the exits stopped. A run with the fix is
**not yet measured**: `0.1.55.7-windows-x86_64-cuda-cuda13` (manifest `2e41b956f1b7ad2ce58514945635802b2c18f32e66ac6a4dd67b0f1487dad586`)
is the package of a build that compiled the changed file - its `strata.exe` is md5 `dc713aa1ec3a85366c319dba0b48c3b0`
(the staged exe, `CMakeFiles/strata.dir/src/program/generate.cpp.obj` compiled at 03:17), and its start prints
`restored N tokens (live)` for a disk read. The `null`-content case above ends in the same line 14823, so one branch
covers both exits.

**2026-11-03, the fixed engine measured on the crash path.** The engine restarted on `0.1.55.9-windows-x86_64-cuda-cuda13`
(manifest `ff57b9f98c1236b079a23c0149a64c31d931381879a6b9dbfc16e746db41a2ac`, `build_id f7fa6c4fcbf2`, the engine string
`strata 0.1.55-local`) at 00:24:18. A scripted traffic run on it: one 25 458-token conversation, five 48-50k ones to
fill the RAM cache, then a continuation whose assistant text differs from the parked one - the prompt diverges inside
the entry's live key, so the read lands on a state below it. The log lines:
`conversation disk: new entry: no stored conversation is a prefix of this one's 25458 tokens`,
`conversation disk: disk hit: 25458 tokens, 557 MiB in 1 file, 596 ms (980 MB/s)`,
`conversation cache: restored 25458 tokens (live) in 42.6 ms` - the mount the old code looked for is not there, the
restore lands as live, the request answered (`prompt_tokens=25485`, wall 1.5 s), and its second run answered again in
0.4 s. Zero `restoring a conversation checkpoint failed`, zero exits, `0/0 writes/reads failed, 0 reads fell back on
damage` over the run. After the restart, every disk read in the log prints `(live)`; the last
`the engine stopped unexpectedly` line in the server log is from 00:14, before this engine started.

**The live append measured on the same engine, and an id defect it found.** The next run parked a 25 412-token
conversation, filled the cache with 48-50k ones, and continued it with its real assistant turn: the continuation's
write `wrote 25504 tokens (appended): 782 MiB in 3 blocks (1 new kv, 0.4 MiB; 2 new state, 225.2 MiB), index 0.5 MiB
rewritten, 1 file, 469 ms (1749 MB/s)` - the entry got the delta, as the plan says. Its prompt replay read back from
the RAM cache (`restored 25428 tokens (live)`, `prompt 31548 tokens = 25428 reused + 6120 read`). The last request,
which asked for the appended entry after the cache filled, printed
`conversation disk: 31541 tokens not restored (the entry is gone (its write failed, or the budget removed it));
reading the prompt` and read the prompt from 0 (`prompt 31548 tokens = 0 reused + 31548 read in 13500 ms`). The
reason: `put` gives the queued image an id (`conversation_disk.cpp:1232`), `best` reports a match by it, and the
writer's publish of a new entry took **another** id (line 1305, `updated.id = ++next_id_`). The published entry is
the one the match named, and a read that waits for its file by the old id finds nothing. The fix publishes the entry
under the id `put` gave it; the store test adds the case: a match of the entry being written, then
`take(a_now.id, 400, ...)` after the write ends, which reads the published entry back
(`the published entry keeps the id of its queued image`). `conversation_disk_test`: **232 checks passed** (230
before, 2 added); the rest of the offline suite is as in step 9 (183 / 4865 / 972 / 4854 / 119). Reading the
appended entry from the disk under this fix on hardware is **not yet measured**.

**The append's id map, and a reader-release defect the new test found.** The fix above covers a write that starts a
new entry. An append is the other half: `put` gives the queued image its own id, the append publishes under the
**target** entry's id, and the queued id stops existing. A read that matched the queued image while the writer ran
loses its entry - measured on `0.1.55.9` as
`conversation disk: 208307 tokens not restored (the entry is gone (its write failed, or the budget removed it));
reading the prompt`, the prompt read from 0. The tier now records the pair (the queued id, the entry published under
its id) at publish, and `take()` follows it, so a read by the queued id finds the entry; `clear()` clears the map
with the entries. The store test covers it: a 600-token image queued behind a gated writer, a 1200-token image
behind it, `best` matches the queued image by its own id, and after the write ends `take(q.id, 1200, ...)` finds the
entry. The live state and both K/V layers come back byte for byte, and the target entry's states stay as the first
write wrote them (an append skips a state block it already holds at that token count), so the checkpoints that read
back are the 600-, 300- and 150-token states of the first write, deepest first.

The same test found a second defect: `take()` counts its reader on the entry it resolved through the map (the
target's id) and released it by the id `put` gave the queued image - a lookup that finds nothing, so the reader's
stamp never goes down, and `clear()`, which waits for `readers == 0`, hangs. The first run of the new test hung for
2 minutes with 0.19 s of CPU time. The release is now keyed by the resolved entry's id, the test passes, and after
`clear()` a later read by the old queued id reports `the entry is gone`.

**Phase 9, measured on `0.1.55.10`** (manifest `676c8e567af01fdedb12400f0dcc5ad6d38d69a011135a8739eaba64e4b22a98`,
`build_id 031e85249dab`, started 00:37:27, engine string `strata 0.1.55-local`). The start line:
`conversation disk: on: E:\strata-conv-data, budget 76800 MiB, persistent (budget only): 42 conversations from
earlier runs (34085 MiB), 0 refused (...)` - the whole folder of the earlier runs taken in, nothing refused, nothing
removed for the budget (the budget is what the two models' stores share on this disk). A 25 460-token conversation
is parked and written, the cache is filled with 48-50k conversations, then its continuation with the assistant turn:
`new entry: no stored conversation is a prefix of this one's 31646 tokens` - the replayed assistant text tokenizes
differently from the tokens generated for it, so no stored chain is a prefix and the write starts a new entry. The
cold read of that entry: `conversation disk: disk hit: 31574 tokens, 498 MiB in 1 file, 602 ms (866 MB/s)`,
`conversation cache: restored 31574 tokens (live) in 60.9 ms`,
`prompt 31581 tokens = 31574 reused + 7 read in 840 ms (8.3 tok/s)`. Over the run's last totals line:
`11 queued, 11 written (10601 MiB, 276 MB/s), 0 RAM hits, 5 disk hits (7259 MiB, 392 MB/s), 502722 prompt tokens
given back, 5 waits for RAM (23824 ms), 0 removed for the budget, 0/0 writes/reads failed, 0 reads fell back on
damage; on disk 53 (44696 MiB), queue 0` - and **zero** `not restored` lines in the whole run: the `the entry is
gone` failure is gone from the log. The append's delta write on this engine is **not yet measured** (the live
append measured on `0.1.55.9`, `wrote 25504 tokens (appended): 782 MiB in 3 blocks (1 new kv, 0.4 MiB; 2 new state,
225.2 MiB), index 0.5 MiB rewritten, 1 file, 469 ms (1749 MB/s)`, needs the id map to be read back on hardware).

`conversation_disk_test`: **240 checks passed** (232 before, 8 added). The rest of the offline suite after this
change: file 183, validation 972, cache 4854, session_save_reclaim 119, snapshot 4865 - the snapshot's card checks
fail on a run started while the serving model holds the card (`six more restores allocate nothing on the card`) and
pass on a rerun, measured on this PC 2026-11-03.

**Phase 10: the append's delta and the cold read of the appended entry, measured on `0.1.55.11`** (manifest
`ae272cabc26195e6ec286bfa9b0bb9194b4a6ed10deb19af1071d65cd9e24d2d`, `build_id 75dea3551a51`, started 06:52:24, engine
string `strata 0.1.55-local`). The start line: `budget 76800 MiB, persistent (budget only): 62 conversations from
earlier runs (53866 MiB), 0 refused (...)`, 0 removed for the budget, 0 stale files. A conversation of 79 700 tokens
was read from the disk (`disk hit: 79700 tokens, 1474 MiB in 1 file, 1603 ms (965 MB/s)`,
`restored 79700 tokens (live) in 65.0 ms`), continued with its assistant turn, and its parked image went to the disk
as an append: `wrote 81047 tokens (appended): 1598 MiB in 2 blocks (1 new kv, 11.6 MiB; 1 new state, 112.6 MiB),
index 2.1 MiB rewritten, 1 file, 115 ms (14596 MB/s)` - **two** blocks for a 1 347-token continuation, the entry's
older blocks copied, not rewritten. The cold read of that appended entry: `disk hit: 81047 tokens, 1598 MiB in 1 file,
12226 ms (137 MB/s)` (the writer was busy with parallel writes), `conversation cache: restored 81047 tokens (live) in
328.8 ms` - the appended state lands as live, the request is served. Over the whole run on this engine: **zero**
`not restored` lines, `0/0 writes/reads failed, 0 reads fell back on damage`.

**A rule the scripted phase 10 showed.** The disk tier's queue is fed by the RAM cache's **evictions**
(`generate.cpp:10590`, `conversations.set_on_evict(...)`): a parked conversation is written when the cache evicts it,
not when it is parked. A scripted continuation whose image stayed in the cache (the cache evicted the longer
fillers, which were older) was never queued and never read from the disk: the request that asked for it read back
from the cache (`restored 32355 tokens (checkpoint)`, `prompt 32362 tokens = 32355 reused + 7 read in 210 ms`).
So the scripted test must fill the cache past the parked image to make the disk path run; that is phase 11.

**Phase 11: the disk path driven by evictions, and why a scripted continuation is not an append.** N1's 26 176-token
turn is parked; six 48k conversations evict it: `queued 26240 tokens (564 MiB in RAM until written; queue 1, 564 MiB)`,
`wrote 26240 tokens: 564 MiB in 16 blocks (13 new kv, 225.8 MiB; 3 new state, 337.8 MiB), index 0.3 MiB rewritten,
1 file, 770 ms (767 MB/s)`. N2 reads it back: `disk hit: 26240 tokens, 564 MiB in 1 file, 610 ms (968 MB/s)`,
`restored 26240 tokens (live) in 27.5 ms`, `prompt 32362 tokens = 26240 reused + 6122 read in 3688 ms (1659.9 tok/s)`.
Its parked image reaches the disk after seven more fillers: `queued 32428 tokens (730 MiB in RAM until written)`, and
its write is a **new entry**, not an append: `new entry: no stored conversation is a prefix of this one's 32428
tokens` - the whole image is written again, `wrote 32428 tokens: 729 MiB in 20 blocks (16 new kv, 279.1 MiB;
4 new state, 450.4 MiB), index 0.4 MiB rewritten, 1 file, 4351 ms (176 MB/s)`. The last request read that entry:
`disk hit: 32355 tokens, 617 MiB in 1 file, 451 ms (1434 MB/s), after 3373 ms waiting for its write`,
`restored 32355 tokens (live) in 30.0 ms` - the read waited for the writer, landed as live, no `the entry is gone`.
Over the run on this engine: `not restored` = 0, `0/0 writes/reads failed, 0 reads fell back on damage`.

**The prefix rule, measured offline.** The chain check compares the stored entry's keys against the new image by
**token ids and image keys** (`ConversationImageKey {start, hash}`; `chain_prefix`'s `same_state` at
`conversation_disk.cpp:749`, `prefix_digest` at 354 hashes the ids up to the block's start and every image key with a
start below it). A continuation whose prompt-cache added an image key inside the reused prefix is not a prefix, so its
write starts a new entry. The store test shows both sides: a 600-token entry, then 1200 tokens with no image keys -
one entry, appended; then 1200 tokens with one image key at 300 tokens the chain does not hold - two entries and the
line `no stored conversation is a prefix of this one's 1200 tokens`. `conversation_disk_test`: **249 checks passed**
(240 before, 9 added). The live append measured above (79 700 to 81 047 tokens, two delta blocks) is the case where
the image keys stay the chain's prefix.

**Phase 12: the scripted append measured on the live engine.** Phase 11's fillers were not enough: the cache kept the
continuation's parked image (`restored 40251 tokens (checkpoint)`, `prompt 40258 tokens = 40251 reused + 7 read in
477 ms`), so its image never reached the disk. With twelve fillers after the continuation the eviction happens and the
append runs: the first conversation's image on the disk as a new entry (`wrote 32663 tokens: 619 MiB in 19 blocks
(16 new kv, 281.1 MiB; 3 new state, 337.8 MiB), 2120 ms (306 MB/s)`), the continuation reads it back
(`prompt 40258 tokens = 32660 reused + 7598 read in 10038 ms`, `reused from the disk in 4.6 s`), its parked image goes
to the disk and appends: `wrote 40321 tokens (appended): 797 MiB in 6 blocks (4 new kv, 65.9 MiB; 2 new state,
225.2 MiB), index 0.5 MiB rewritten, 1 file, 3414 ms (245 MB/s)` - six blocks for the delta, the entry's older blocks
copied. The last request read the appended entry from the disk: `disk hit: 40251 tokens, 685 MiB in 1 file, 752 ms
(955 MB/s)`, `restored 40251 tokens (live) in 53.3 ms`, `prompt 40258 tokens = 40251 reused + 7 read in 1154 ms` - the
state the prompt reaches is a checkpoint of the appended entry, it lands as live, no `the entry is gone`. Over the run:
`0/0 writes/reads failed, 0 reads fell back on damage`, `not restored` = 0. The disk was near its budget
(`on disk 81 (74567 MiB)` of 76800 MiB), so `40 removed for the budget` lines appear - LRU removals, not refusals.

**Phase 13: three appends on one scripted chain.** A conversation is continued three times, each continuation
replaying the answer as an assistant turn, with six fillers after each to evict the parked image. The first round
writes a new entry (`new entry: no stored conversation is a prefix of this one's 41323 tokens`); rounds 2 and 3
append on top of it: `wrote 49984 tokens (appended): 1218 MiB in 8 blocks (5 new kv, 74.6 MiB; 3 new state,
337.8 MiB), index 1.1 MiB rewritten, 1 file, 1620 ms (789 MB/s)` and `wrote 58649 tokens (appended): 1518 MiB in 7
blocks (5 new kv, 74.6 MiB; 2 new state, 225.2 MiB), index 1.5 MiB rewritten, 1 file, 3407 ms (467 MB/s)` - the
scripted continuation is a prefix here, so the delta rule holds for it. The reads land on the entry's states as live:
`disk hit: 41252 tokens, 693 MiB in 1 file, 569 ms (1277 MB/s)`, `restored 41252 tokens (live) in 125.4 ms`,
`prompt 41259 tokens = 41252 reused + 7 read`; the second and third reads reuse 41252 and not the appended states
(49 984 and 58 649 tokens), because those live states are above the prompt's length and the deepest state at or below
the prompt is the one a read can rebuild. Over the run: `not restored` = 0, `0/0 writes/reads failed`, and the queue
was never caught: 89 queued and 89 written with **0 RAM hits** - every read came after its write had ended, so no
`after X ms waiting for its write` line appears. Phase 14 makes the queue deep on purpose to catch that path.

**Phase 14: a deep queue, and the writer keeps up.** A 36 697-token conversation is parked (`parked 36763 tokens`,
the cache evicted 90 parked conversations in this run), eight long conversations are asked so its image goes to the
disk, and it is asked twice: after the fillers, and after six more. The disk lines: `queued 36763 tokens (767 MiB in
RAM until written; queue 1, 767 MiB)`, `new entry: no stored conversation is a prefix of this one's 36763 tokens`
(a new entry, the whole image), `wrote 36763 tokens: 767 MiB in 22 blocks (18 new kv, 316.4 MiB; 4 new state,
450.4 MiB), index 0.5 MiB rewritten, 1 file, 8060 ms (100 MB/s)`. The first follow-up read came after the write ended:
`disk hit: 36690 tokens, 654 MiB in 1 file, 722 ms (950 MB/s)`, `restored 36690 tokens (live) in 67.0 ms`,
`prompt 36697 tokens = 36690 reused + 7 read in 1166 ms`. The queue stayed at 1 the whole run - 96 queued, 96
written, **0 RAM hits**, `not restored` = 0, `0/0 writes/reads failed`. So filling the cache does not widen the
window: the writer keeps up with one conversation per park. To read a queued image, the request must come inside its
write window (8 s for a full write of 36 763 tokens, 1.6 s for a 1218 MiB append): phase 15 asks for the conversation
immediately after the park that evicts it.

**Phase 15: the queued match was not caught, and it will be watched in the daily logs.** K15 was parked
(`parked 36763 tokens` in an earlier run; in this run the cache had room, `parked=4`), one long filler did not evict
its image, so no write was queued (`queued 36763 tokens` line absent), and the request that followed read back from
the RAM cache: `restored 36690 tokens (checkpoint) in 81.1 ms`, `prompt 36697 tokens = 36690 reused + 7 read in
340 ms`. Over phases 13, 14 and 15 the queued match was not caught by scripted traffic: 89 writes with 0 RAM hits
(phase 13), 96 writes with 0 RAM hits (phase 14), and phase 15 never queued the image. Chasing the window by hand is
not worth the traffic: the path is covered offline by the store test (the read of the queued image's id finds the
appended entry, `conversation_disk_test` 249 checks). In normal use the log lines to watch are
`RAM hit: N tokens back from the write queue (M MiB, no disk) in X ms`,
`disk hit: N tokens, ..., after X ms waiting for its write` with `restored N tokens (live)`, and the refusal
`N tokens not restored (the entry is gone (its write failed, or the budget removed it))`; every scripted run on
`0.1.55.11` printed zero `not restored` lines.

**The version rule held**: `project(strata VERSION 0.1.55)` is unchanged, the engine names itself `0.1.55-local`,
and the build number lives only in the package folder and manifest (`0.1.55.3-...`). The index identity hash is
built from the settings and the engine's version string (`conversation_file.cpp:891` writes the pair
`"engine", c.engine_version`; `generate.cpp:10507` sets it to `STRATA_VERSION`), so a package folder named
`0.1.55.3` does not empty the store, and the start line reports `0 refused`.

## What each step must not touch

- the session files of the HTTP API (`/slots/0?action=save|restore`) - keep v1 byte-identical,
  every phase;
- the other backends - the tier is not run on HIP, SYCL or Metal ([DETAILS.md](DETAILS.md));
- the default path of a conversation without `--conversation-disk-dir` - byte-identical, as the
  plan requires.
