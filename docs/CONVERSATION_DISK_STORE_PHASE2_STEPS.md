# [Engine] phase 2 in steps: the small plan

This file breaks phase 2 into steps. One step at a time, each one compiles, its tests pass and its
commit stands on its own. What phase 2 is, why this order and what it must not change are in
[CONVERSATION_DISK_STORE_PHASE2.md](CONVERSATION_DISK_STORE_PHASE2.md); phase 1's store is described
in [CONVERSATION_DISK_STORE_PHASE1.md](CONVERSATION_DISK_STORE_PHASE1.md) and its steps in
[CONVERSATION_DISK_STORE_PHASE1_STEPS.md](CONVERSATION_DISK_STORE_PHASE1_STEPS.md); the numbers the
phase answers are in [CONVERSATION_DISK_STORE_MEASUREMENTS.md](CONVERSATION_DISK_STORE_MEASUREMENTS.md).

Phase 1 is complete in code and tests: the tier appends blocks, a read keeps the entry, the budget
evicts whole conversations by use, and a folder of the previous build is removed at start. Phase 2
changes **when** the tier is written and **who owns the bytes**, not the container: the `.bin`
header, the `.idx` index, the block records and the read path stay exactly as phase 1 left them.

Decisions already agreed (2026-11-03):

1. **the flag is `--conversation-disk-flush-seconds N`**, `int64_t`, `0` = off and today's behaviour
   byte for byte, `N` = the debounce in seconds. `1` is the honest "write at every park" mode: no
   special value, no second flag. Validation: `>= 0` only (`src/program/generate.cpp:3260-3277` is
   the numeric group the engine already has).
2. **one cache callback replaces `set_on_evict`.** `ConversationCache::set_on_change(CacheEvent,
   uint64_t serial, std::shared_ptr<const SavedConversation>)` and five events - `parked`, `taken`,
   `evicted`, `superseded`, `cleared` - are the only way the disk tier learns what happened. The
   cache stays request-thread-only by contract; no mutex is added to it.
3. **the image is shared, never copied.** `ConversationCache` holds
   `std::shared_ptr<SavedConversation>` and `take()` returns that pointer. The disk tier holds
   `std::shared_ptr<const SavedConversation>` while a write is queued or running. One allocation, two
   references. `take()` cancels the queued write for that serial **before it returns**; a write that
   is already running is waited for on that serial only (see step 5), because the restore path moves
   stage images and checkpoints out of the image and the writer must not serialize a gutted one.
4. **eviction and QUIT force a full write; a debounced flush is cheap.** A debounced flush writes the
   K/V beyond the last stored `kv` block, page-aligned, and **at most one end state** at the current
   position - only when the end is at least `--prompt-cache-every` tokens (default 16 384) beyond the
   deepest stored state, or when the entry has no state at all. No checkpoint chain in a debounced
   flush. An evicted conversation and the QUIT save write the whole image, chain included, exactly as
   phase 1 does.
5. **a deferred job is not RAM pressure.** `pending()` reports `true` only for a write that is running
   or a job whose due time has passed. A job waiting for its due time owns no RAM of its own (the
   bytes belong to the cache, or to an evicted image the writer already holds). `wait_idle()` collapses
   every due time to now before it waits, so "make room in RAM" and "shut down" never become timer
   waits.
6. **one writer thread, no new thread.** The tier already has exactly one writer
   (`conversation_disk.cpp:1250`); the debounce lives in it.
7. **the default path stays byte-identical.** Without `--conversation-disk-dir`, or with
   `--conversation-disk-flush-seconds 0`, nothing new is queued at park time: the tier is written on
   eviction and at QUIT as in phase 1. The tier's files and format do not change.
8. **the engine's compiled-in version string does not change.** It is part of the conversation
   identity the disk tier matches on (`docs/AI_REFERENCE.md`, the fork's version rule). The build
   number goes in `VERSION`, never in `CMakeLists.txt`'s `project(VERSION ...)`.
9. **testing runs on its own folder.** Phase 1's decision 4 still holds: tests, validation runs and
   manual checks use their own `--conversation-disk-dir` and `--conversation-cache-mib`; nothing in
   the work touches the serving profile's storage until the owner decides to switch.

## The steps

| # | step | files | what it buys alone |
|---|---|---|---|
| 1 | **the gate: shared ownership in the cache** | `include/strata/core/conversation_cache.hpp`, `src/core/conversation_cache_test.cpp`, `src/program/generate.cpp` | `Entry{shared_ptr image, serial}`; `take()` returns the pointer; every accessor derefs; `generate.cpp`'s `incoming` is a `shared_ptr` and every move on the restore path is audited. The hot path (park, take, admission) is proven not to regress before anything else is written |
| 2 | **the cache tells the tier what happened**: `CacheEvent`, `set_on_change`, the five call sites, and the tier's seam with phase 1 semantics | `conversation_cache.hpp`, `conversation_disk.{hpp,cpp}`, `generate.cpp`, `conversation_disk_test.cpp` | the disk tier is driven by events, not by one eviction hook; with flush `0` it behaves exactly as phase 1 (an evicted image is written whole), which is the regression test of this step |
| 3 | **the queue and the clock**: `Job.due/serial/cancelled/forced`, the ordered queue, `wait_until`, replace-by-serial, cancel, `pending()`/`wait_idle()`, and the `--conversation-disk-flush-seconds` flag | `conversation_disk.{hpp,cpp}`, `generate.cpp` | a park is queued at once and written after quiet; eviction and QUIT force the write; a deferred job never shows up as RAM pressure |
| 4 | **what a debounced flush writes**: K/V delta, one end state per `--prompt-cache-every`, no chain; a forced write keeps phase 1's whole-image shape | `conversation_disk.{hpp,cpp}`, `generate.cpp`, `conversation_disk_test.cpp` | a flush every few seconds costs ~27-31 MiB of K/V instead of ~118 MiB of states, and a write still says in the log which shape it took |
| 5 | **reads cancel writes**: `taken`/`superseded` drop the handle, a queued image comes back as the same allocation (no copy), a read of a job being written waits for it, and the id map keeps a match valid across an append | `conversation_disk.{hpp,cpp}`, `generate.cpp`, `conversation_disk_test.cpp` | a park that is read back before its deadline leaves no file, and a read during a write frees the bytes once |
| 6 | **the Windows writer priority** | `src/core/conversation_disk.cpp` | the writer stops competing with decode: `THREAD_PRIORITY_BELOW_NORMAL`, not `IDLE`, beside the Linux `PRIO_PROCESS 10` |
| 7 | **flag plumbing in the launcher and the server** | `strata_cli/launch/settings.py` (check only), `serve/server.py`, `tests/terminal/test_launch.py` | the preset → launch-file → engine round trip keeps the number; the QUIT wait knows a debounce can be pending |
| 8 | **the tests of the plan, 1-10** | `conversation_disk_test.cpp` | debounce, reset, read cancels, eviction bypasses, QUIT bypasses, one state per flush, growth threshold, shared ownership, take during a write, flag `0` |
| 9 | **the docs, the log line and the two numbers the log gets wrong** | `docs/DETAILS.md:814-869`, the writer's log, `conversation_disk.{hpp,cpp}`, `generate.cpp` | the tier's section describes the debounce, the flag and the two write shapes; one log line per flush says K/V versus state and why a state was written; the summary and the totals count the bytes appended, not the published entry; and `not read` weighs the blocks a read needs, not the entry's size |

Steps 2 and 3 may be written in one branch (the event seam is not exercised until the queue exists),
but they land as separate commits. Step 4 needs step 3's forced/debounced split; step 5 needs step 1's
shared pointer and step 3's cancellation. Step 8's tests 8 and 10 gate steps 1 and 2 respectively and
are written with them; the rest arrive with step 8, because they need the whole path.

## Step by step

### Step 1 - the gate: shared ownership in the cache

`ConversationCache`'s entry becomes:

```cpp
// include/strata/core/conversation_cache.hpp:563, from std::deque<SavedConversation>
struct Entry { std::shared_ptr<SavedConversation> image; uint64_t serial; };
std::deque<Entry> entries_;   // least recently active first
```

The `serials_` deque goes: the serial lives in the entry. What else moves:

- `take()` (`:340`) returns `std::shared_ptr<SavedConversation>`: it moves the pointer out, subtracts
  `image->bytes()`, erases the entry and returns the pointer. It does **not** move the image apart;
  `generate.cpp` does that, and only after step 2's `taken` event has cancelled a queued write.
- `put(SavedConversation&& image, size_t held)` keeps its signature for this step and wraps the image
  in `std::make_shared`; step 3 adds the overload that takes the allocation the park already made.
- every accessor derefs: `best()`, `holds()`, `borrows()`, `index_of()`, `find()`, `drop_superseded()`,
  `evict_oldest()`, `make_room()`, `longest_tokens()`, `bytes()`, `pinned()`.
- `find(serial)` still returns `const SavedConversation*` (`entry.image.get()`), so `generate.cpp`'s
  borrow path is untouched.
- accounting does not change: the cache counts `image->bytes()`, as today. The extra reference the
  disk tier takes in step 2 adds nothing.

In `src/program/generate.cpp`:

- `std::optional<SavedConversation> incoming` (`:14382`) becomes
  `std::shared_ptr<SavedConversation> incoming` (null = none); `incoming.has_value()` becomes
  `incoming != nullptr`, `incoming.emplace(x)` becomes `incoming = x`, `incoming.reset()` is unchanged.
- the restore path's moves are audited one by one: `live` and its images (`:14742-14743`), the checkpoint
  chain (`:14745`, `:14748-14749`), the later stages' K/V (`:14757`) and `retain(std::move(incoming->kv),
  ...)` (`:14758`). Each is safe only because the writer no longer holds the image: step 1 has no
  writer sharing yet, so the audit is a list for step 2 to keep true.

The gate's test, in `conversation_cache_test.cpp` (test 8 of the plan):

- `bytes()` does not change when a park is followed by a `take()` and a re-`put()`;
- the image's `use_count` is 2 while a second owner holds it and 1 after that owner lets go;
- the existing 4 854 checks of the cache test stay green, and `conversation_disk_test`,
  `conversation_file_test`, `conversation_validation_test`, `session_save_reclaim_test` and the three
  snapshot tests are unchanged.

The hot path is the gate because a `shared_ptr` touches every park, take and admission: the park's own
log line (`conversation cache: parked ... in N ms`) is the number to compare, before and after, on the
same day.

### Step 2 - the cache tells the tier what happened

The callback, next to today's `set_on_evict` (`:428`):

```cpp
enum class CacheEvent { parked, taken, evicted, superseded, cleared };
void set_on_change(std::function<void(CacheEvent, uint64_t serial,
                                     std::shared_ptr<const SavedConversation>)> fn);
```

| event | call site | the disk tier does |
|---|---|---|
| `parked` | `put()` (`:509`) | flush `0`: nothing; flush `N`: arm the due time and hold the handle (step 3) |
| `taken` | `take()` (`:340`) | drop the handle; cancel a queued job; step 5 returns the allocation itself |
| `evicted` | `evict_oldest()` (`:413`) | drop the handle, queue with `due = now`, forced whole-image write |
| `superseded` | `drop_superseded()` (`:453`) and `put()`'s own call (`:512`) | drop the handle, cancel |
| `cleared` | `clear()` (`:432`) | cancel everything |

`set_on_evict` is removed; `generate.cpp:10601` registers `set_on_change` and switches on the event.
At the end of this step the tier's public surface is four event methods beside `put()`:
`park(serial, image)`, `dropped(serial)`, `force(serial, image)`, `cancel_all()`. With flush `0`,
`park()` is a no-op and `force()` is phase 1's `put()`: this is test 10 of the plan, the regression
that says the default path did not move.

### Step 3 - the queue and the clock

`Job` (`conversation_disk.hpp:219-222`) gains:

```cpp
std::chrono::steady_clock::time_point due;   // when the writer may start it
uint64_t serial = 0;                         // the cache entry this job came from
bool cancelled = false;
bool forced = false;                         // eviction or QUIT: write the whole image, chain included
std::shared_ptr<const SavedConversation> image;
```

The queue becomes `std::multimap<std::chrono::steady_clock::time_point, Job>` ordered by `due`
(`:238-239`), holding at most a handful of entries. `writer()` (`:1250`) changes its wait from
`cv_.wait(lock, ...)` to a loop around `cv_.wait_until(lock, queue_.begin()->first)`: drop cancelled
jobs, write the ones whose `due` has passed, compute the next deadline, wait again. A second park of
the same `serial` replaces the queued job instead of appending a second one; the newer image is
strictly better and keeps the id the first park gave it.

`pending()` (`:1697`) reports `writing_` or a job with `due <= now` only (decision 5).
`wait_idle()` (`:1702`) collapses every due time to now before it waits, so a deferred job never turns
a shutdown or a "make room" into a timer wait. `clear()` (`:1731`) drops queued and deferred jobs,
then waits for a running write and every reader, as today.

The flag: `--conversation-disk-flush-seconds N` in `generate.cpp`'s numeric group (`:3260-3277`) and
`ConversationDiskOptions::flush_seconds`; `serve/server.py:1074-1076` already reads the persist flag
for the QUIT wait, and gets the debounce's bound beside it. Nothing in `strata_cli/launch/settings.py`
needs a list: only word-valued flags are special (`:280-284`), and a numeric flag goes through as a
value. `tests/terminal/test_launch.py` gets the round-trip case (step 7).

Tests 1, 2, 4 and 5 of the plan arrive here: park → nothing before the deadline → written after;
two parks 20 ms apart with 100 ms write once with the second image; an eviction with `flush = 3600`
writes at once; `wait_idle()` with `flush = 3600` writes everything and returns quickly.

### Step 4 - what a debounced flush writes

`write()` (`conversation_disk.cpp:1276`) takes the job's shape from `job.forced`:

- **forced** (phase 1, unchanged): all `kv` ranges beyond the last stored block, every checkpoint and
  the end state, `merge_keys()` over the whole image, supersede and publish as today.
- **debounced**: the same `kv` ranges; the state list is `{&image.live}` only, and only when
  `tokens - deepest_stored_state >= prompt_cache_every` or the entry holds no `state` block in a
  stage. No checkpoint chain. The entry's keys gain the end state's key only when that state was
  written, so `best()` never claims a prefix no `state` block covers; the K/V past it is readable,
  the state is what a read lands on.

`ConversationDiskOptions` gains `prompt_cache_every` (default 16 384, `generate.cpp:889`), copied from
`--prompt-cache-every`. The write's log line says which shape it was (`debounced` or `forced`) and,
for a debounced flush that skipped the state, how far past the deepest stored state it is - that is
the number the validation uses to decide "one state per flush" against "the whole chain".

Tests 6 and 7: two debounced flushes at 200k and 260k tokens write one state each and the second only
because the growth passed 16 384; a flush that adds 1 000 tokens writes K/V and no state.

### Step 5 - reads cancel writes, and a queued image is handed back whole

- `taken` and `superseded` erase the job with that `serial` under the lock; a job that has not started
  owns no file, so nothing is left behind (test 3: the file is not created).
- a queued job that matches a read is handed back as the same allocation:
  `take(id, tokens, std::shared_ptr<SavedConversation>& out, error, admit)` copies nothing and moves
  the queued `shared_ptr` out (an "RAM hit", as today).
- a write already running for that serial is waited for before the cache may move anything out of the
  image (`writing_serial_`); this is the one place step 1's move audit needs the writer's cooperation.
  The wait is bounded by one write and happens only when a request takes the very conversation the
  writer is flushing. Test 9 covers the ownership: the reader gets the image, the writer finishes, the
  bytes are freed once.
- the `published_` id map (`:247`, `:1540-1542`) keeps a match on a queued id valid when the append
  publishes under the entry's id; it is cleared with the entries, as today.

`generate.cpp`'s disk restore path (`:14417`) passes `incoming` as the shared pointer directly, so a
queued image never becomes a second copy.

### Step 6 - the Windows writer priority

In `writer()` (`conversation_disk.cpp:1250`), beside the Linux `setpriority(PRIO_PROCESS, 10)`:

```cpp
#if defined(_WIN32)
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL);
#endif
```

Not `THREAD_PRIORITY_IDLE`: below normal is deprioritized against decode without starving on a busy
system. `#include <windows.h>` stays inside the `_WIN32` block, as the rest of the tree does.

### Step 7 - flag plumbing in the launcher and the server

- `tests/terminal/test_launch.py`: a preset with `--conversation-disk-flush-seconds 5` survives the
  preset → settings → launch-file round trip, and `0` is a real value (not a removed flag), because
  the engine's `0` is the off switch and a launcher that drops it changes the meaning.
- `serve/server.py:1074-1076`: the QUIT wait stays 300 s when persist is on; the debounce adds at most
  `N` seconds to the last flush, so it is part of the bound the server already grants and not a new
  timer. A line in the log says which flush mode the engine was started with.
- no change to `WORD_VALUE_FLAGS` (`strata_cli/launch/settings.py:284`): the new flag is numeric.

### Step 8 - the tests of the plan, 1-10

All in `conversation_disk_test.cpp` (CPU only), with the existing `before_write` hook and 50-100 ms
debounce values, plus the cache-side test 8 in `conversation_cache_test.cpp`:

| # | test | lands with |
|---|---|---|
| 1 | debounce: park, nothing before the deadline, written after; `stats().written` is 0 meanwhile | step 3 |
| 2 | reset: two parks 20 ms apart with 100 ms write once, with the second image | step 3 |
| 3 | read cancels: park, read before the deadline, no write, the file is not created | step 5 |
| 4 | eviction bypasses: `flush = 3600`, evicting writes at once | step 3 |
| 5 | QUIT bypasses: `flush = 3600`, `wait_idle()` writes everything and returns quickly | step 3 |
| 6 | one state per debounced flush: 200k then 260k tokens, one state each, the second only past 16 384 | step 4 |
| 7 | growth threshold: a flush adding 1 000 tokens writes K/V and no state | step 4 |
| 8 | shared ownership: `bytes()` unchanged on a park, `use_count` 2 then 1 after the write, a park read back before its write leaves no file | steps 1 and 5 |
| 9 | take during a write: the reader gets the image, the writer finishes, the bytes are freed once | step 5 |
| 10 | flag 0: a park writes nothing, an eviction writes the whole image (phase 1 behaviour) | step 2 |

The file's header gets the same note phase 1 has: which block of the file is which plan test, because
the blocks were written one step at a time.

### Step 9 - the docs and the log

- `docs/DETAILS.md:814-869`: the tier's section gains the park-time write and the debounce
  (`--conversation-disk-flush-seconds`), the two write shapes, eviction and QUIT forcing a write, and
  the one rule a reader must know: a deferred job is not RAM pressure.
- the writer's log: one line per flush with its shape, the K/V and state bytes it added, and the
  tokens since the deepest stored state. The validation's "bytes written per day, split by K/V and
  states" comes from these lines.
- **the write line's MiB and MB/s must be the bytes appended, not the published entry.** Measured on this
  PC 2026-11-03/04: `bin_bytes` sums every block of the entry (`conversation_disk.cpp:1592-1598`) and
  `stats_.bytes_written` adds it, so an append logs `wrote 88948 tokens (appended): 1666 MiB in 2 blocks
  (1 new kv, 9.6 MiB; 1 new state, 112.6 MiB), 204 ms (8554 MB/s)` while the device wrote 122.2 MiB in
  204 ms (~600 MB/s). Over the phase-1 runs the log reports 383 782 MiB written and 323 955 MiB went to
  the disk. The per-block lines are already right; the summary and the totals line have to count the
  delta. This is code in `conversation_disk.cpp` with its tests, not a docs change.
- **the `not read` estimate must count the blocks a read needs, not the entry's bytes.** `Match.bytes` is
  the entry's `.bin` plus its index (`conversation_disk.cpp:1592-1598`), and `generate.cpp:14424` divides
  it by the mean read speed. Measured on the live runs: seven refusals quote a "file" of 1600, 2116, 2371
  and 2460 MiB for 7539-7586-token reads, and the smaller-cache run refused one read of 764 added tokens
  whose "file" was 1494 MiB, while the reads that ran took exactly the blocks below the state they landed
  on (ratio read/needed 1.00, 87-100% of the entry). The rule stays; the number it compares has to be the
  bytes below the deepest state a read can land on. Code in `conversation_disk.{hpp,cpp}` and
  `generate.cpp`, tested beside step 8.
- [CONVERSATION_DISK_STORE_PHASE2.md](CONVERSATION_DISK_STORE_PHASE2.md) is not edited: this file
  records where each step stands, as phase 1's steps file does.

## Build and test on this PC

The offline suite (`./.venv/Scripts/python.exe -m pytest tests/terminal -q`) does not compile the
engine. The conversation tests are generated only with CUDA on (`CMakeLists.txt`'s conversation block),
as in phase 1:

```sh
VS="C:/Program Files (x86)/Microsoft Visual Studio/2022/BT2022"
C="$VS/Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"

"$C" -S C:/Projects/Strata-fork -B C:/Projects/Strata-fork/build-c13-gate -G "Visual Studio 17 2022" -A x64 \
     -DSTRATA_ENABLE_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=native -DSTRATA_BUILD_CONVERSATION_TESTS=ON
"$C" --build C:/Projects/Strata-fork/build-c13-gate --config Release --target conversation_disk_test conversation_cache_test
```

`build-c13-gate` is phase 1's folder and is reusable while its cache is valid. The engine itself is
built with `python3 packaging/build_engine_win_local.py engine` (a running server does not block it);
the version string must not change (decision 8). A restart of the serving model is asked for, never
done unasked.

## Where each step stands

2026-11-03. **Step 1 is done, and it is green.** The cache holds the image's bytes by
`std::shared_ptr` instead of owning the only copy; `take()` hands the allocation over; the restore
path in `generate.cpp` carries the image as a `std::shared_ptr` and still moves sub-objects out of it
(the move list below is what step 2's cancel-on-take rule has to keep safe). Nothing is queued at
park time yet: `set_on_evict` still drives the disk tier, so the tier's behaviour is phase 1's.

What landed:

- `include/strata/core/conversation_cache.hpp`: `Entry{std::shared_ptr<SavedConversation> image,
  uint64_t serial}` replaces the `std::deque<SavedConversation>` plus the parallel `serials_` deque;
  `take()` returns `std::shared_ptr<SavedConversation>`; every accessor derefs (`best()`, `holds()`,
  `borrows()`, `index_of()`, `find()`, `drop_superseded()`, `evict_oldest()`, `make_room()`,
  `longest_tokens()`, `supersedes_richer()`, `parked_prefix_tokens()`); `put()` gains the
  `shared_ptr` overload and the old `SavedConversation&&` one wraps in `std::make_shared`.
  `evict_oldest()` moves the image out of the shared pointer into `set_on_evict` - safe while the
  cache is the only owner, which is true until step 2 shares a parked image with the writer.
- `src/program/generate.cpp`: `incoming` is `std::shared_ptr<SavedConversation>`;
  `incoming.emplace(...)` became an assignment of `conversations.take(...)` or
  `std::make_shared<SavedConversation>(std::move(image))` for a disk read; `incoming.has_value()`
  became `incoming != nullptr`; the "return the skipped snapshot" path puts the `shared_ptr` back
  directly. The moves on the restore path are unchanged and still run on an image the cache no longer
  holds (`live.ids`/`imgs`, `checkpoints`, `stage_images`, `kv`, and `retain(std::move(incoming->kv))`).
- `sycl/src/program/generate.cpp`: the same two-line change where it takes a parked conversation,
  because it includes the shared header. The SYCL backend is not built on this PC.
- `src/core/conversation_cache_test.cpp`: the gate's test (plan test 8) - the cache holds the same
  allocation (`use_count() == 2`), a park counts its bytes once, a round trip
  park -> take -> park leaves the byte accounting unchanged and the pointer identical, and after the
  outside owner lets go the image has one reference.

Measured on this PC (build folder `build-c13-gate`, Release, the serving model running):

- `conversation_cache_test`: **4 862 checks, exit 0** (the 4 854 phase 1 measured plus the gate's 8).
- `conversation_disk_test`: **258 checks, exit 0** - the tier is untouched and still driven by
  `set_on_evict`.
- `conversation_file_test`: **183 checks, exit 0** (the golden sample unchanged);
  `conversation_validation_test`: **972 host-only checks, exit 0**;
  `session_save_reclaim_test`: **119 checks, exit 0**.
- the engine: `--target strata` built, exit 0; the only warnings are the tree's own C4244 template
  lines in `generate.cpp`.

The park's duration was not measured: the engine was not restarted (the serving model was left
running), so the hot path's number is the next check before step 2 - with the same traffic, compare
`conversation cache: parked ... in N ms` before and after this change.

Next was step 2: the `CacheEvent` callback and the tier's event seam, with flush `0` keeping phase 1's
behaviour.

2026-11-03. **Step 2 is done, and it is green.** The cache reports what happened to a parked
conversation through one callback; the disk tier is driven by those five events and no longer by a single
eviction hook. With flush `0` (the only mode until step 3 adds the flag) the behaviour is phase 1's: a
park queues nothing, an eviction writes the whole image, and a taken or superseded conversation's queued
write is cancelled.

What landed:

- `include/strata/core/conversation_cache.hpp`: `enum class CacheEvent { parked, taken, evicted,
  superseded, cleared }` and `set_on_change(std::function<void(CacheEvent, uint64_t serial,
  std::shared_ptr<const SavedConversation>)>)`; `set_on_evict` is gone. The events fire from `put()`
  (`parked`, after the entry is admitted), `take()` (`taken`, before the image is returned),
  `evict_oldest()` (`evicted`), `drop_superseded()` (`superseded`, which `put()`'s own pre-admission
  call also reaches) and `clear()` (`cleared`, serial `0`). The image handed to the callback is the shared
  allocation, never a copy.
- `include/strata/core/conversation_disk.hpp` / `src/core/conversation_disk.cpp`: the tier's event
  surface - `park(serial, image)` (a no-op at flush `0`), `dropped(serial)` (erases that serial's queued
  job), `force(serial, image)` (phase 1's `put()`, whole image) and `cancel_all()` (the queue goes) -
  beside `put(SavedConversation&&)` for callers without a cache entry (`put()` is now `force(0, ...)` over
  a `make_shared` of the moved image; `generate.cpp`'s RAM-refused park still uses it).
  `Job` holds `std::shared_ptr<SavedConversation> image` and the entry's `serial`. The shared pointer is
  mutable on purpose: the cache hands the allocation out as `const`, the tier recovers the mutable handle
  for the one case that needs it - `take()` hands a queued image back to the restore path as the same
  allocation (step 5) - and the writer itself only reads it. `write()` no longer moves the image apart
  (it renders `const SavedConversation*` views of the first image and each stage image, and drops its
  handle when the write ends, so the cache, if it still holds it, keeps the bytes).
  `chain_prefix()` takes those views too.
- `src/program/generate.cpp`: registers `set_on_change` and switches on the event (`parked` -> `park` when
  disk writes are on, `taken`/`superseded` -> `dropped`, `evicted` -> `force`, `cleared` -> `cancel_all`).
  The QUIT path (`persist`) still sets `disk_writes = true`, parks the current conversation and evicts the
  rest, which forces the writes as before.
- `src/core/conversation_disk_test.cpp`: the cache-driven block now uses `set_on_change`, and it is plan
  test 10 - a park writes nothing (`stats().written == 0`, no file), an eviction writes the whole image,
  and `take()` reports `taken` before handing the image over. The file's header says so.

Measured on this PC (build folder `build-c13-gate`, Release, the serving model running, no restart):

- `conversation_cache_test`: **4 862 checks, exit 0** (unchanged from step 1).
- `conversation_disk_test`: **260 checks, exit 0** (258 from step 1 plus the two assertions of test 10).
- `conversation_file_test`: **183 checks, exit 0**; `conversation_validation_test`: **972 host-only
  checks, exit 0**; `session_save_reclaim_test`: **119 checks, exit 0**.
- the engine: `--target strata` built, exit 0; the only warnings are the tree's own C4244 template lines
  and the C4324 padding notes in the CPU kernel headers.

The park's duration is still not measured: the serving model was left running (a restart is the owner's
call), so the hot path's before/after number for steps 1 and 2 stays the first check to make when it is
restarted.

Step 3 is next: the queue and the clock (`Job.due`, `wait_until`, replace-by-serial, `pending()`/
`wait_idle()`) and the `--conversation-disk-flush-seconds` flag. `park()` becomes real there; nothing else
in this step has to change.

2026-11-03. **Step 3 is done, and it is green.** The queue is ordered by due time and the writer sleeps
until the earliest one; `--conversation-disk-flush-seconds N` arms a park's write `N` seconds out,
eviction and QUIT force it at once, and a deferred job never reads as RAM pressure. The flag defaults to
`0`, which is step 2's behaviour (and phase 1's) byte for byte.

What landed:

- `include/strata/core/conversation_disk.hpp`: `ConversationDiskOptions::flush_seconds` (`0` = off);
  `Job.due`, `Job.forced` beside `serial`; the queue is
  `std::multimap<std::chrono::steady_clock::time_point, Job>`; `writing_serial_` records the serial the
  writer is on; `enqueue()`/`enqueue_image()` are the one place a job is admitted, given an id and logged.
- `src/core/conversation_disk.cpp`:
  - `park(serial, image)` is a no-op at flush `0`; otherwise it queues the shared image with
    `due = now + N s`. `force(serial, image)` is `due = now`. Both go through `enqueue()`, which replaces
    an earlier job of the same serial (the newer image wins, and the id the first park gave it is kept, so
    a read that matched the queued id still finds the entry).
  - `dropped(serial)` erases that serial's queued job; if the writer is already on that serial it waits
    for that one write (the caller is about to move sub-objects out of the shared image), and says so in
    the log. This is the safety step 5 described; it is here because step 3 turns the flag on.
  - `writer()` waits on `cv_.wait_until(queue_.begin()->first)`, re-reads the queue on every wake, and
    writes the earliest due job.
  - `pending()` is `writing_` or a job whose `due` has passed. `wait_idle()` makes every deferred job due
    now (again on every wake, so a park arriving during the wait cannot hold it up) and then waits.
  - `clear()` drops queued and deferred jobs, then waits for the running write and every reader, as before.
- `src/program/generate.cpp`: `--conversation-disk-flush-seconds N` (nonnegative integer, in the existing
  numeric group; a negative value is refused with the group's message), the usage line, and
  `disk_opts.flush_seconds`. The flag is numeric, so `strata_cli/launch/settings.py` needs no entry
  (`WORD_VALUE_FLAGS` lists word-valued flags only). `serve/server.py`'s 300 s QUIT wait already covers
  the debounce, because QUIT collapses every due time; the log line beside it is step 7.
- `src/core/conversation_disk_test.cpp`: plan tests 1 (debounce), 2 (reset), 4 (eviction bypass) and 5
  (QUIT bypass), plus a `wait_until` helper. The debounce is one second in the tests (the flag is in
  whole seconds), so the blocks take about a second each.

Deliberate departures from the plan's wording:

- `Job.cancelled` is not a field. A cancelled job is erased from the queue under the same mutex the
  writer pops under, so there is no window in which a cancelled job is visible, and `pending()`/`best()`
  need no filter. The observable behaviour the field was for (a taken or superseded entry is not
  written) is what `dropped()` does.
- The wait for a running write of the same serial (`writing_serial_`) landed here rather than in step 5,
  because step 3 is where the debounce starts writing images the cache still owns. Step 5's remaining
  work is its tests (3, 8, 9) and the id-map check across an append.
- Until step 4, a debounced write has the forced shape (the whole image, chain included): `job.forced` is
  recorded and ignored by `write()`. Step 4 adds the K/V-only + one-state shape.

Measured on this PC (build folder `build-c13-gate`, Release, the serving model running, no restart):

- `conversation_disk_test`: **281 checks, exit 0** (260 from step 2 plus the 21 of tests 1, 2, 4 and 5).
- `conversation_cache_test`: **4 862 checks, exit 0**; `conversation_file_test`: **183 checks, exit 0**;
  `conversation_validation_test`: **972 host-only checks, exit 0**; `session_save_reclaim_test`:
  **119 checks, exit 0**.
- the engine: `--target strata` built, exit 0. `--help` shows the new flag, and
  `--conversation-disk-flush-seconds -1` is refused with "needs a nonnegative integer within range".

Step 4 is next: what a debounced flush writes (K/V delta, at most one end state per
`--prompt-cache-every`, no checkpoint chain), while a forced write keeps the whole-image shape.

2026-11-03. **Step 4 is done, and it is green.** A debounced flush now writes the K/V it is missing
and at most one end state; a forced write (eviction, QUIT) is still phase 1's whole image. The write's
log line says which shape it took, and a debounced flush that skipped the state says how far past the
deepest stored state it is.

What landed:

- `include/strata/core/conversation_disk.hpp`: `ConversationDiskOptions::prompt_cache_every` (default
  16 384), set from `--prompt-cache-every` in `generate.cpp`. `0` means a state at every debounced flush.
- `src/core/conversation_disk.cpp`:
  - `write()` computes `write_state = job.forced || !target || tokens - deepest_state >= prompt_cache_every`
    before it touches the keys. `deepest_state` is the target entry's `tokens` (its deepest `state` block);
    a new entry has none, so its first flush always writes the end state.
  - the state list is `{&image.live}` for a debounced flush and live-plus-checkpoints for a forced one. A
    new entry's keys are the end state alone (forced: plus the chain). An existing entry's keys gain the
    end state only when it was written (`merge_live_key()`, `merge_keys()` minus the image's checkpoints),
    so `best()` never claims a prefix no `state` block covers.
  - `updated.tokens` is left alone when no state was written: the entry still claims only its deepest
    stored state, while the K/V past it is on the disk for the next state to cover.
  - the `wrote ...` line ends with `forced write, the whole image` or `debounced write, the end state
    written` or `debounced write, no end state (N tokens past the deepest stored state, under
    --prompt-cache-every)`.
- `src/core/conversation_disk_test.cpp`: plan tests 6 and 7 in the "shape" block. The numbers are scaled
  down - 600, 1200 and 1300 tokens with `prompt_cache_every = 512`, instead of 200k/260k with 16 384 - so
  the test does not build 200k-token K/V on a CPU run. What it proves is the rule: the first flush writes
  exactly one state (the end) and no checkpoints, the second adds one state because the growth passed the
  threshold, the third adds K/V only because it did not; `best()` keeps returning the stored state, and a
  read lands on it.

Measured on this PC (build folder `build-c13-gate`, Release, the serving model running, no restart):

- `conversation_disk_test`: **296 checks, exit 0** (281 from step 3 plus the 15 of tests 6 and 7).
- `conversation_cache_test`: **4 862 checks, exit 0**; `conversation_file_test`: **183 checks, exit 0**;
  `conversation_validation_test`: **972 host-only checks, exit 0**; `session_save_reclaim_test`:
  **119 checks, exit 0** (the forced path is unchanged, so phase 1's tests still describe it).
- the engine: `--target strata` built, exit 0.

Step 5 is next: reads cancel writes - `taken`/`superseded` drop the handle, a queued image comes back as
the same allocation, the id map keeps a match valid across an append, and tests 3, 8 and 9. The
running-write wait and the no-copy queued handback are already in (steps 2 and 3), so step 5 is mostly
its tests and the id-map check.

2026-11-04. **Step 5 is done, and it is green.** A read that lands on a still-queued image now gets that
allocation itself - `take()` returns a `std::shared_ptr<SavedConversation>` and moves the queued pointer
out, so generate.cpp's `incoming` is the cache's image, not a second object. A queued write is cancelled
by `taken`/`superseded`, a write already running for that entry is waited for (step 3's
`writing_serial_`), and a read before the deadline leaves no file.

What landed:

- `conversation_disk.hpp` / `conversation_disk.cpp`: `take(id, tokens, std::shared_ptr<SavedConversation>&
  out, error, admit)`. `out` is reset first. A queued job hands its own `shared_ptr` over (no copy, the
  same allocation the cache parked); a written entry is read into a fresh image, which is moved into a
  `shared_ptr` (one small allocation, the payload is moved). Every failure path leaves `out` empty.
- `src/program/generate.cpp`: the disk restore path passes `incoming` to `take()` directly and drops the
  `make_shared` that wrapped the read. `parked.tokens` comes from `incoming->live.ids.size()`.
- `src/core/conversation_disk_test.cpp`: a `take_image` helper keeps the older blocks readable (it moves
  the payload out of the shared image), so the twenty-odd call sites did not turn into allocation
  assertions. Plan tests 3 and 8 are the "cancel" block: a park shares the allocation (`use_count 2`), a
  read before the deadline returns the same pointer, no file appears, `ram_hits` counts it, and the image
  is held once afterwards. Test 9 is the "take-writing" block: the writer is held inside `before_write`,
  `dropped(serial)` waits for exactly that write instead of letting the reader gut the image, and when it
  returns the writer has published the entry and the tier's handle is gone (`use_count 1`).
  The id map across an append is phase 1's test ("append id"), unchanged and still green.

Measured on this PC (build folder `build-c13-gate`, Release, the serving model running, no restart):

- `conversation_disk_test`: **311 checks, exit 0** (296 from step 4 plus the 15 of tests 3, 8 and 9);
  three runs in a row, because two blocks use real threads and deadlines.
- `conversation_cache_test`: **4 862 checks, exit 0**; `conversation_file_test`: **183 checks, exit 0**;
  `conversation_validation_test`: **972 host-only checks, exit 0**; `session_save_reclaim_test`:
  **119 checks, exit 0**.
- the engine: `--target strata` built, exit 0 (only the tree's existing C4996/C4324 warnings).

Step 6 is next: the Windows writer priority (`THREAD_PRIORITY_BELOW_NORMAL` beside the Linux
`PRIO_PROCESS 10`).

2026-11-04. **Steps 6, 7, 8 and 9 are done, and they are green.** The writer is below normal priority on
Windows as it is on Linux; the launcher and the server carry the debounce flag through; the plan's ten
tests are all in; and the tier's section, its log line and the two numbers the log got wrong are fixed.
Nothing in this block changes what is written or when: a forced write is still phase 1's whole image and
a debounced one still appends K/V plus at most one end state.

What landed:

- **step 6**: in `writer()` (`conversation_disk.cpp`), `#if defined(_WIN32)` sets
  `SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL)` beside the Linux
  `setpriority(PRIO_PROCESS, ..., 10)`. `#include <windows.h>` sits in the file's `_WIN32` block with
  `WIN32_LEAN_AND_MEAN` and `NOMINMAX`, as the rest of the tree does. Not `THREAD_PRIORITY_IDLE`: below
  normal is deprioritized against decode without starving on a busy system.
- **step 7**: `serve/server.py` reads `--conversation-disk-flush-seconds` beside
  `--conversation-disk-persist` and logs one line with the mode the engine was started with
  (`a park is written at eviction and QUIT only` or `N s after quiet`). The QUIT wait is unchanged at
  300 s: QUIT collapses every due time, so a pending debounce is inside that bound and is not a timer of
  its own. `tests/terminal/test_launch.py` has the round-trip case: `--set conversation-disk-flush-seconds=5`
  and `=0` both become `--conversation-disk-flush-seconds <value>` on the engine line and survive the
  plan's `extra` and `flags`; `0` stays a value, because the engine's `0` is the off switch.
  `WORD_VALUE_FLAGS` is untouched: the flag is numeric.
- **step 8**: the plan's ten tests are all in `conversation_disk_test.cpp` (they landed with steps 2-5,
  one block each; test 8's cache half is in `conversation_cache_test.cpp`), and the file's header says
  which block is which plan test.
- **step 9**, docs: `docs/DETAILS.md`'s tier section gains the park-time write and the debounce
  (`--conversation-disk-flush-seconds`, `0` default = phase 1's behaviour), the two write shapes (a
  debounced flush = the K/V past the last stored block and at most one end state per
  `--prompt-cache-every`, no chain; eviction and QUIT = the whole image), the rule that a deferred job is
  not RAM pressure, the Windows priority, and the two corrected numbers.
- **step 9**, the write line (`conversation_disk.cpp`): the MiB and MB/s of `wrote ...` and
  `stats_.bytes_written` now count the bytes this write appended - the new blocks and the header of a
  file it creates - not the entry's published size. Phase 1 summed every block of the entry, so an append
  of 122.2 MiB logged 1666 MiB and the totals over a day's runs said 383 782 MiB where 323 955 MiB went
  to the disk. The line also ends with the tokens past the deepest stored state (or "the entry held no
  state before"), which is the number "one state per flush" is checked against, and the shape
  (`forced`/`debounced`) was already there.
- **step 9**, the `not read` estimate (`conversation_disk.{hpp,cpp}`, `generate.cpp`):
  `conversation_disk_read_bytes(entry, limit)` returns what a read of the entry lands on reads - the
  deepest `state` block at or below `limit`, the K/V below it and the states at or below it - and
  `Match::bytes` is that number, not `Entry::bytes` (the `.bin` plus its index). A phase 2 entry whose
  debounced flushes appended K/V past its deepest stored state no longer looks bigger than the blocks a
  read needs, so the comparison against `ds.read_ms / ds.bytes_read` in `generate.cpp` weighs the blocks
  the read actually touches; the refusal's line says "the blocks that read needs" instead of "its file".
  A queued image keeps the image's in-RAM estimate.

Measured on this PC (build folder `build-c13-gate` in the session's worktree, Release, the serving model
running, no restart; configured with `-DFETCHCONTENT_SOURCE_DIR_STRATA_LLAMACPP` pointed at the main
checkout's populated llama.cpp, so nothing was downloaded):

- `conversation_disk_test`: **313 checks, exit 0** (311 from step 5 plus step 9's two: an append's total
  is the appended bytes and not the entry's file, and `best().bytes` is below the file for an entry with
  a K/V tail past its stored state).
- `conversation_cache_test`: **4 862 checks, exit 0**; `conversation_file_test`: **183 checks, exit 0**;
  `conversation_validation_test`: **972 host-only checks, exit 0**; `session_save_reclaim_test`:
  **119 checks, exit 0**.
- the engine: `--target strata` built, exit 0.
- `pytest tests/terminal -q`: **2 041 passed, 62 skipped, 7 failed** in 32 min. The 7 are exactly the
  ones `docs/LOCAL_FORK_CHECKS.md` lists as this PC's known failures (four `test_launch_dsv41` plan
  cases, `test_private_files`' locked file, `test_bundle_imports`' missing `third_party/llama.cpp`,
  `test_launch_tui`'s `FolderStep`); `test_launch.py` passes, the new round-trip case included.

Still not measured: the park's duration and the debounce's effect on live traffic. Both need the serving
model restarted on this build, and a restart is the owner's call - the serving model was left running
through every step.

Phase 2's steps, 1-9, are complete in code and tests.

2026-11-04. **A review after step 9 found three real problems; they are fixed and green.** The reviewer was
asked to read phase 2 against the spec (its report is not in the repo). What it found and what changed:

- **the park path could wait a whole background write** (the spec's gate). `ConversationCache::put()` calls
  `drop_superseded()` on every park, that fires `superseded`, and `generate.cpp` sent both `taken` and
  `superseded` to one `dropped(serial)` which waits for the running write of that serial. With the debounce
  on, the writer is usually writing the copy a turn back - exactly what `drop_superseded` drops - so the
  next park stalled. The spec says the wait happens only when a request takes the very conversation the
  writer is flushing, and that is right: only the restore path moves sub-objects out of the shared image.
  `dropped(serial, bool wait_running = true)` now separates the two; the `taken` event waits, the
  `superseded` event passes `false` and only cancels. Its log line says so ("waited N ms for the write of
  N tokens to end"), because a superseded conversation's write is not cancelled when it has begun - it ends.
- **`wait_idle()` could miss a wakeup.** The RAM-hit branch of `take()` and the cancel path of `dropped()`
  erased queued jobs without `cv_.notify_all()`, while `wait_idle()` waits on a bare `cv_.wait(lock)`.
  Unreachable with one request thread, a latent hang with two. Both paths notify now.
- **the server's log line could name a mode that never runs.** `serve/server.py` printed the flush mode
  whenever persist was not `off` or the debounce was not `0`, without checking `--conversation-disk-dir`;
  without the folder the tier is off. The line now needs the dir flag.
- small ones: `conversation_disk_read_bytes()` adds the one `.bin` header per stage that
  `read_entry_image()` charges, so the "not read" estimate is the read's own number; and
  `--conversation-disk-flush-seconds` is capped like the `INT32_MAX` flags instead of the numeric group's
  `INT64_MAX / (1024*1024)`, which could overflow `steady_clock` when added to `now()`.
- tests: "superseded" (a cancel returns while the write still runs), "cancel-deferred" (the cache's cancel
  of a still-queued write leaves no file at all), and test 10's second half (a forced write keeps the
  checkpoint chain and says `forced`), which the review called out as only half-checked.

Decided on 2026-11-04 — the review's QUIT point, **kept as it is**:

- **QUIT and persist do not write a pinned conversation as the whole image.** `evict_oldest()` skips an
  entry holding a pinned shared prefix (and the guarded donor), so the QUIT loop never forces it: with
  flush `N` its deferred job is collapsed by `wait_idle()` and written in the debounced shape, and with
  flush `0` it is not written at all. This is phase 1's behaviour for flush `0`; the debounced shape is
  the new part, and it contradicts "the QUIT save writes the whole image" for a conversation the budget
  keeps for its pin. **No QUIT-time pass over the cache's entries.** That fix changes the one path where
  the engine waits for the disk synchronously at the end of its life, it sits inside the server's 300 s
  QUIT bound (`serve/server.py:1084`), and it breaks "the default path stays byte-identical"
  (CONTRIBUTING). The store is a cache and a pin is part of it: a crash costs one longer prompt read, so
  buying a whole-image write at QUIT for a prefix the budget refuses to trade is not worth changing the
  shutdown path. The behaviour is now tested, not only written: the blocks "pinned quit debounced" (the
  pinned conversation gets one end state and no chain, the evicted one gets the forced whole image) and
  "pinned quit off" (the pinned conversation is not written at all) in
  [src/core/conversation_disk_test.cpp](../src/core/conversation_disk_test.cpp); [DETAILS.md](DETAILS.md)
  names the exception in the text. Run on this PC (Windows 10, CPU-only test, `build-c13-gate` with
  `-DSTRATA_BUILD_CONVERSATION_TESTS=ON`) the same day: `conversation_disk_test` **341 checks passed**
  (the two blocks add 16 `check()`s), `conversation_cache_test` **4862 checks passed**.

Not fixed, recorded here as the reviewer's remaining points:

- **`published_` grows for the whole run** (one pair per append, pruned only by `clear()`), and `take()`
  scans it linearly. Small, but unbounded.
- **`write()` calls `chain_prefix()` for every entry under the tier's mutex**, and that recomputes the
  prefix digest for every block from token 0. A debounce writes far more often than an eviction did, so
  the request thread's `best()`/`take()` queue behind it more often. Not measured: it is the hot-path
  number the restart is for.
- the queued-image handback still relies on the caller not moving sub-objects out of an image the cache
  also holds (today `added > 0` keeps that path unreachable); the invariant lives in `generate.cpp`.

## The first hardware numbers (2026-11-04)

`tools/agent_turns_bench.py` ran the scripted coding-agent traffic (5 sessions, 23 turns, prompts from
2 199 to 53 668 tokens, the same requests on both arms) on this PC (RTX 4080 16 GB, 48 GB RAM,
Windows 10, engine `0.1.55.13-windows-x86_64-cuda-cuda13`, base config `strata-swift-iq2_xs.json`,
cache 6144 MiB / 8 slots / park minimum 16384 tokens), one round, each arm on a fresh store folder with
the same budget:

| arm | flush | writes | shapes | K/V | states | index | park median | park p95 | park max | queue waits | decode median |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | `0` | 1 | 1 forced whole image | 1064 MiB | 788 MiB | 2 MiB | 313.4 ms | 797.0 ms | 4500.4 ms | 1 wait, 4297 ms | 133.1 tok/s |
| B | `5` | 10 | 9 debounced (9 wrote a state), 1 forced | 4796 MiB | 1689 MiB | 5 MiB | 305.2 ms | 1523.7 ms | 1636.8 ms | 1 wait, 679 ms | 133.8 tok/s |

- **the gate the plan sets for `shared_ptr` passes:** 16 parks in each arm (the same turns park the same
  conversations), and the park's median is the same on both arms — 313.4 ms against 305.2 ms;
- **the request path did not slow down:** B is x1.016 against A on the per-turn decode median
  (12 of 23 turns faster), inside the run-to-run movement this harness reports;
- A wrote once because exactly one conversation was evicted; B wrote ten because every park gets its own
  debounced flush. The bytes are K/V-dominated: 4796 MiB K/V against 1689 MiB states. The growth past the
  deepest stored state was 52 573 tokens (median), max 123 621 — far over `--prompt-cache-every` 16384,
  which is why 9 of the 10 flushes wrote a state;
- B's park p95 is worse (1523.7 ms against 797.0 ms), from one long park. One round cannot tell that from
  noise: the same run needs more rounds before this number means anything;
- **this traffic does not cover the read path: 0 disk hits, 0 `not read`, 0 RAM hits in both arms.** The
  23 turns never ask for a conversation the cache has given up, so the queue's RAM hits, `not read`,
  waits for RAM and supersede are untouched. The read gates need traffic that fills the cache with several
  conversations and then continues an evicted one, as phase 1's scripted runs did;
- QUIT is not in these numbers: the harness ends the server with `terminate()`, which sends no QUIT. QUIT's
  own duration comes from stopping the serving profile (this PC: 1.5 s and 16.8 s on the two restarts of
  this day, in [CONVERSATION_DISK_STORE_MEASUREMENTS.md](CONVERSATION_DISK_STORE_MEASUREMENTS.md) the
  earlier numbers are 3.9 s for 13 entries and 17.0 s on the 2x CMP box).

The table is what `tools/phase2_disk_ab.py` reads out of the engine's own log: it splits a log at each
`engine started:` line, takes the flags that run started with, and counts the shapes, the bytes split, the
park durations, the waits, the reads and refusals, and the QUIT line. `--csv` writes one row per run.

### The read path, measured on purpose-built traffic (2026-11-04)

The same two arms, on traffic made to exercise the read gates: `tools/phase2_disk_traffic.py` grows 4
conversations to ~100 000 prompt tokens each (one request per ~8000-character tool result, so the RAM cache
fills and gives conversations up), asks for the first grown conversation by its stored prefix, continues it
with one new tool result, sends a conversation the tier never held, and asks for the first conversation
again after the append. 242 parks, ~380 requests per arm, prompts 137 to 105 227 tokens, on this PC and the
same engine, cache 6144 MiB / 8 slots, each arm on its own fresh store folder with a 60000 MiB budget:

| arm | flush | writes | shapes | K/V | states | index | disk hits | 'not read' | RAM hits | park median | park p95 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | `0` | 2 | 2 forced whole image | 1737 MiB | 1576 MiB | 4 MiB | 1 (1544 MiB, 100 860 tokens) | 1 refusal, 132 tokens | 0 | 286.0 ms | 409.4 ms |
| B | `5` | 8 (3 appended) | 3 forced, 5 debounced (5 wrote a state) | 4381 MiB | 2590 MiB | 9 MiB | 1 (1545 MiB, 100 912 tokens) | 1 refusal, 132 tokens | 0 | 281.4 ms | 389.1 ms |

- **the debounced flush is where an append lives:** B appended to entries the disk already held 3 times, A
  never appended (its writes were the evictions). The bytes a debounced run moves are bigger than the
  evictions-only run's — 6971 MiB against 3313 MiB — because it writes the K/V delta at every quiet period;
  5 of B's 8 writes wrote a state, and the growth past the deepest stored state was ~100 900 tokens median
  (max 105 274), far over `--prompt-cache-every`;
- **the queue stays shallow under the debounce.** The reviewer's worry that a debounce writes far more often
  is right about the count, not the depth: 242 parks queued 219 jobs (`queued` is a counter, not a depth),
  and every park line reported `queue 1`, because a new park of the same conversation replaces its queued job.
  The writer never fell behind a depth of 1, which is the number measured on 0.1.55.11 before this;
- **the park duration does not get worse with the debounce**: median 281.4 ms against 286.0 ms, p95 389.1
  against 409.4 (the single worst park is B's, 452.9 ms against A's 1049.8 ms; the same traffic parks the
  same conversations);
- reuse is the same on both arms: 17.88M tokens reused against 18.04M, 466 976 tokens read again against
  467 341, over ~380 requests;
- **two gates are still not covered by any scripted traffic: RAM hits (0 on both arms) and supersede (0
  writes reporting a superseded entry).** A RAM hit needs the tier's queue to hold a conversation the cache
  has already given up, so the disk's queue must carry a longer prefix than the cache — and in this traffic
  losing a conversation from the cache is always an eviction, which forces its write. So a queued
  (deferred) image can be taken back only when the request arrives inside the quiet window before the
  writer starts; these runs never arranged that ordering, which is why B's 242 deferred parks gave 0 RAM
  hits. The same allocation handback and the "a take during a write waits for that write" are covered by
  the offline blocks `conversation_disk_test` ("take-writing", "cancel"); the hardware numbers for them need
  traffic that parks a conversation, evicts it without forcing, and re-asks its prefix inside the window.
  A supersede needs a continuation whose image keys change, which the phase 1 run produced by giving the
  assistant text of a turn that differs from the parked one. QUIT is outside these runs: the harness ends
  the server with `terminate()`, which sends no QUIT.

The tool that grew this traffic records `usage.prompt_tokens` for every request. The first version tracked
the size by `timings.prompt_n`, which counts only the tokens a request prefilled (the reused KV is not in
it); on this PC such a loop grew one conversation to 259 886 tokens and the server answered the next request
with "request exceeds the context window: prompt (262165 tokens) leaves no room to answer in the context
(262144)". The engine was fine; the loop was wrong, and the fix tracks the whole prompt.

## Live day on the serving profiles (2026-11-05/06)

Both serving profiles ran the phase 2 build (engine `0.1.55.14-windows-x86_64-cuda-cuda13`) for a full day
of real agent requests on this PC (RTX 4080 16 GB, 48 GB RAM, Windows 10), on the serving store folder
`E:\strata-conv-data` with the 76 800 MiB budget: `iq2_xs-low_ram` with `--conversation-disk-flush-seconds 60`
and `--conversation-cache-mib 4096` from 05:03 to the QUIT at 15:00, and `iq2_xs-max_ram` with the flag at its
default `0` and `--conversation-cache-mib 8192` from 12:01. The second is **running now** (port 8080, its log
written to 02:31 on 2026-11-06), so its numbers are the run as it stands at the scan, and its QUIT line comes
when the server stops. The launch file `strata-swift-iq2_xs.max_ram.json` gained the flag `60` at 14:59 on
2026-11-05, during the run; the server keeps the flags it loaded, so the next start of this profile is a
debounced one. The arms are not a controlled A/B — different hours, different RAM caches, the same store
folder — and every number names its run.

| arm | flush | cache | requests | writes (appended) | shapes | K/V | states | index | past median | park median | park p95 | park max | QUIT | disk hits | `not read` | waits for RAM |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| low_ram | 60 | 4096 MiB | 793 | 97 (54) | 51 forced, 46 debounced (40 wrote a state) | 40 701 MiB | 23 759 MiB | 392 MiB | 10 819 | 148.9 ms | 773.5 ms | 1242.9 ms | 3.1 s | 43 | 0 | 1, 2050 ms |
| max_ram | 0 | 8192 MiB | 3166 | 44 (4) | 44 forced | 47 132 MiB | 31 415 MiB | 133 MiB | 127 397 | 146.1 ms | 960.6 ms | 8515.0 ms | stop timed out (300 s) | 29 | 8 | 5, 16 092 ms |

- **the park gate passes on live traffic**: median 148.9 ms with the debounce against 146.1 ms without it
  (159 and 594 parks), p95 lower with it (773.5 against 960.6 ms), and the worst park is the flush-0 arm's
  (8515.0 against 1242.9 ms). The phase 1 quoted median, 234 ms, is under both arms.
- **the debounced flush writes its delta**: median tokens past the deepest stored state 10 819 against
  127 397, and 54 of the 97 writes appended to entries the disk already held (4 of 44 in the flush-0 arm).
- **the QUIT day ends with almost nothing to write**: `saved for the next start in 3.1 s` with 51 entries and
  75 497 MiB on disk, one 2050 ms wait for 2 queued writes; the flush-0 runs in this log QUIT at the same
  store size in 15.3, 16.8, 26.4 and 29.8 s (2026-11-03). The flush-0 arm is running, so its QUIT number
  comes when that server stops.
- **the stop of the flush-0 arm says the same in the other direction**: stopping it on 2026-11-06 gave
  `server_stop_deadline_expired` — its QUIT did not fit the server's 300 s window (the run held ~44 entries
  of a 76 GiB store, and the log has no `saved for the next start` line for it). A debounced day reaches
  QUIT in 3.1 s; a full-store flush-0 burst does not reach it at all inside the stop window. This PC's
  clock moved during the day (log timestamps of 23:35-23:39 sit among file stamps of 02:3x), so the
  section's times are read from the lines the engine printed, and the restart the owner's other session
  made came with no flush flag: the live instance is flush 0, `--conversation-cache-mib 8192`, and the
  launch file's `60` still applies at its next start.
- **the corrected `not read` estimate holds on live traffic**: 0 refusals in the debounced day against 8
  (2373 tokens) in the flush-0 run; the reads took the blocks below the state they landed on.
- **restore time, the number phase 3 says to watch**: 43 disk hits, 127 422 MiB, mean 982 MB/s; median
  3.2 s, p95 5.2 s, max 6.9 s (154 871 tokens, 4598 MiB, 701 MB/s); the 43 `reused from the disk` lines say
  median 3.4 s, max 7.1 s. A ~150K-token conversation restores in the same time band as the 5.6 s at
  643 MB/s on the 2x CMP box, on this PC's SSD.
- **supersede fires in RAM, not on the disk**: the cache dropped 37 superseded copies (flush 60) and 135
  (flush 0); disk-side supersede is 0 in all 141 writes, as phase 1 measured for its 403.
- **RAM hits are 0 on both arms**: no live request read back a queued image inside its quiet window, so the
  queued-image handback and the running-write wait stay proven offline only (the blocks "take-writing" and
  "cancel").
- **waits for RAM go down with the debounce**: 1 (2050 ms) against 5 (16 092 ms).
- The store after the day, read at 02:23 on 2026-11-06 with `tools/conversation_store_scan.py` — the `.idx`
  reader of the plan, committed here so phase 3's re-run uses the same tool: 38 entries, 76 539 MiB, K/V
  46 917 MiB, states 29 500 MiB, 5 451 442 tokens; the duplicated prefix **13 706 MiB = 29.2% of the K/V,
  17.9% of the store**, down from the 2026-11-04 morning snapshot (36.3% and 22.2%); the biggest pair shares
  127 726 tokens (1792.5 MiB); the shared system prompt band 6731-7798 tokens; 0 entries fully inside
  another, 0 pinned state blocks; 6-9 states per conversation, 7 in 32 of 38 — the chain rule's count, the
  phase 3 window is untested.

## Risks and open questions

- **The hot path.** `shared_ptr` in `ConversationCache` touches every park, take and admission. Any
  regression there costs more than the whole feature saves; the park's own log line is the gate.
- **Moving out of a shared image.** The restore path moves stage images and checkpoints
  (`generate.cpp:14742-14758`). The cancel-on-take rule is what keeps the writer out of a gutted
  image; step 1 lists every move and step 5 makes the running-write case wait.
- **`pending()` semantics.** A deferred job must never read as RAM pressure: getting this wrong turns
  the debounce into a periodic `wait_idle()` from `make_ram`, precisely the stall this phase removes.
- **The write shape.** A debounced flush skips the checkpoint chain, so a conversation restored from
  the disk re-reads its tail from the last stored state. The `not read` count and the tokens read
  again after the restored checkpoint (`generate.cpp:14379`) are the numbers that decide whether to
  change that in phase 3; phase 2 starts with "one state per flush".
- **Crash value is zero.** The store is a cache: a crash costs one longer prompt read. That is why `0`
  (off) is a defensible default and why the debounce can be long.
- **Static analysis, unconfirmed.** Everything here about the current code is read, not run, except
  the numbers quoted from the measurements and [DETAILS.md](DETAILS.md). Phase 2 is measured in part on
  this PC's live traffic (the section "Live day on the serving profiles", 2026-11-05/06): the park
  durations, the write shapes, the QUIT, the reads, the store scan.
