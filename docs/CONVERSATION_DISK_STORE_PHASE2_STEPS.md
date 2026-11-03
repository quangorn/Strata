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
| 9 | **the docs and the log line** | `docs/DETAILS.md:814-869`, the writer's log | the tier's section describes the debounce, the flag and the two write shapes; one log line per flush says K/V versus state and why a state was written |

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
  the numbers quoted from the measurements and [DETAILS.md](DETAILS.md). No part of phase 2 is
  measured on hardware yet.
