# [Engine] the disk tier, phase 2: a park goes to the disk at once

A design note, not a change: no code is touched in this document. Phase 2 depends on phase 1 (the
append-only block store, [CONVERSATION_DISK_STORE_PHASE1.md](CONVERSATION_DISK_STORE_PHASE1.md)):
without it, writing at park time would multiply the bytes instead of spreading them. Agree the scope
in an issue before any code (CONTRIBUTING).

## The problem

The disk tier is written from one trigger only: the RAM cache giving up. `set_on_evict` is armed at
`src/program/generate.cpp:10585`, and the other path is QUIT (`:17789-17797`: park the current
conversation, evict every parked one, wait for the writes). Both are late:

| cost | where | measured (CONVERSATION_DISK_STORE_MEASUREMENTS.md, RTX 4080 16 GB, one day of agent sessions) |
|---|---|---|
| a conversation is written whole at the moment its RAM is needed | `conversation_disk.cpp:367` | 47 writes, 88 306 MiB, 91.7 s on the SSD the tier now uses |
| the evicted image holds its RAM until its write ends, so a park can wait | `:407`, `make_ram` at `generate.cpp:10605` | 4 waits at 1.4-2.7 s |
| a read of an entry being written waits for the write | `:459-464` | included in the 42 reads, 77 679 MiB |
| QUIT is one synchronous burst | `generate.cpp:17789-17797` | 3.9 s on this PC with 13 entries (20 981 MiB) on the disk; 17.0 s for 5 786 MiB on the 2x CMP 50HX box (`DETAILS.md:856-860`) |

The measurement also says the disk is not a within-run cache at all: with `--conversation-disk-persist
exit-only`, 40 writes and 197 GiB in a day were never read back inside the run, and both hits came
from the QUIT save (`DETAILS.md:852-854`). A store for the next start does not need the eviction
trigger; it needs the writes to be cheap enough to do early.

## Scope

In:

- `put()` on every park, not only on eviction;
- the image shared between the RAM cache and the writer instead of copied (`shared_ptr` entries);
- a debounce that lives in the writer thread, not in the request path;
- a read cancelling a pending write;
- eviction and QUIT forcing the write regardless of the timer;
- the Windows thread priority, below normal.

Out:

- the container and the block table: phase 1;
- deduplication, reference counts, garbage collection: phase 3;
- any new thread: the tier already has exactly one writer (`conversation_disk.cpp:290`, `:341-365`).

Default path unchanged: the tier stays opt-in, and the new flag defaults to `0`, which is exactly
today's behaviour.

## Design

### 1. Shared ownership of the image

Today `evict_oldest()` moves the entry out (`conversation_cache.hpp:418-421`) and the writer owns it
until the write ends. At park time that would mean either a copy of the image (about 1.2 GiB per
park, at the moment the cache is trying to keep RAM) or taking the image out of the cache (which
changes what `best()`, `borrows()`, `drop_superseded()` and the budget see).

So the cache holds the image by `std::shared_ptr`:

```cpp
// conversation_cache.hpp:563, from std::deque<SavedConversation>
struct Entry { std::shared_ptr<SavedConversation> image; uint64_t serial; };
std::deque<Entry> entries_;
```

and the disk tier keeps a `std::shared_ptr<const SavedConversation>` while a write is pending. One
allocation, one owner of the bytes, two references to it. Every accessor derefs: `best()`, `take()`,
`borrows()`, `index_of()`, `find()`, `drop_superseded()`, `evict_oldest()`, `make_room()`, `bytes()`.

Accounting does not change: the cache counts `image->bytes()` as today, the disk tier's reference adds
nothing. When the cache drops its reference on eviction, the writer's reference keeps the RAM alive
until the write ends — the same lifetime as today, so `pending()`/`wait_idle()`'s meaning
(`conversation_disk.cpp:517-534`) is preserved.

`take()` (`conversation_cache.hpp:340`) hands out the shared pointer instead of moving the image out.
`generate.cpp` keeps `std::optional<SavedConversation> incoming` (`:14366`); it becomes
`std::shared_ptr<SavedConversation>` and the restore reads through it. **Requirement:** `take()`
cancels the pending write before it returns, because the restore path may move sub-objects (stage
images, checkpoints) out of the image, and a writer must never serialize an image that was taken
apart. This one rule is what makes sharing safe.

### 2. The cache tells the disk tier what happened

There are five places where an entry's fate changes, and the disk tier needs all five. One callback
with an event, next to `set_on_evict` (`conversation_cache.hpp:428`):

```cpp
enum class CacheEvent { parked, taken, evicted, superseded, cleared };
void set_on_change(std::function<void(CacheEvent, uint64_t serial,
                                     std::shared_ptr<const SavedConversation>)> fn);
```

| event | call site | the disk tier does |
|---|---|---|
| `parked` | `put()` (`:509`) | arm the due time, hold the handle |
| `taken` | `take()` (`:340`) | drop the handle, cancel |
| `evicted` | `evict_oldest()` (`:413`) | drop the handle, queue with due = now |
| `superseded` | `drop_superseded()` (`:453`) and `put()`'s own call (`:512`) | drop the handle, cancel |
| `cleared` | `clear()` (`:432`) | cancel everything |

The alternative — the disk tier looking entries up in the cache when it is ready — needs a mutex in
the cache, which is request-thread-only today; rejected, with the caveat that `ConversationCache` is
not thread-safe and phase 2 does not make it so.

### 3. The queue and the clock

`Job` (`conversation_disk.hpp:126-129`) gains `due` (`std::chrono::steady_clock::time_point`),
`serial` and `cancelled`. The queue is kept ordered by `due` (a `std::multimap<time_point, Job>`; it
holds at most a handful of entries). The writer loop becomes:

```cpp
// conversation_disk.cpp:349-350, from cv_.wait(lock, ...)
cv_.wait_until(lock, queue_.front().due);
```

On wake: drop cancelled jobs, write the ones whose `due` has passed, compute the next deadline, wait
again. A second park of the same `serial` replaces the queued job instead of appending a second one —
the newer image is strictly better.

### 4. What a debounced flush writes

- the K/V of the tokens beyond the last stored `kv` block, page-aligned, in phase 1's pieces;
- **one** end state at the current position, written only when the end is at least
  `--prompt-cache-every` tokens (default 16 384, `generate.cpp:884`) beyond the deepest stored state,
  or when the entry has no state at all. A flush every few seconds then costs ~27-31 MiB of K/V and no
  state, instead of 118 MiB;
- no chain in a debounced flush. This is a deliberate starting point: the chain is 7 blobs, and its
  only cost to skip is a longer tail to re-read when the conversation is restored from the disk. The
  log line (below) is what decides whether to change it.

### 5. Eviction and QUIT force everything

- `evict_oldest` → `evicted`: `due = now`, and the flush writes the **whole image**, chain included,
  exactly as phase 1 does at eviction. The RAM is leaving, so there is nothing to gain by waiting.
- QUIT (`generate.cpp:17789-17797`) sets `disk_writes`, parks the current conversation, evicts the
  rest and calls `wait_idle()`. `wait_idle()` (`:522-534`) collapses every due time to now before
  waiting, so a deferred job never turns "make room in RAM" or "shut down" into a timer wait.
- `pending()` (`:517`) must not report a deferred job as "the writer is holding RAM": a job that is
  waiting for its due time owns no extra RAM (the bytes belong to the cache or to an evicted image
  that the writer holds). It reports `true` only for a job whose write is running or is due now.

### 6. One number for the flag

```
--conversation-disk-flush-seconds N     int64_t, 0 = off (default), N = the debounce in seconds
```

`0` is today's behaviour, so the default path is byte-identical. `N = 1` is the honest "write at
every park" mode: the image is already in the queue, and the writer waits one second of quiet for
that conversation — no special value, no second flag. Phases above this one use the house rule
`0 = off` (`--prompt-cache-every`, `generate.cpp:884`).

Plumbing: the argument and its help in `generate.cpp` (next to `:3267-3275`) plus validation
(`>= 0`); `strata_cli/launch/settings.py` needs no list for numeric flags (only word-valued ones are
special, `:284`); `serve/server.py:1074` reads the persist flag for the QUIT wait and needs to know
about the debounce; `tests/terminal/test_launch.py` gets a case that the flag survives the preset →
launch-file round trip.

### 7. Windows priority

`writer()` sets `PRIO_PROCESS 10` on Linux (`conversation_disk.cpp:342-344`). Add
`SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL)` under `_WIN32`. Not
`THREAD_PRIORITY_IDLE`: below normal is deprioritized against decode without starving on a busy
system. This is the "some day" item the plan calls out; it ships with phase 2 because that is when
the writer starts working while a request is being served.

## Code map

| file | what changes |
|---|---|
| `include/strata/core/conversation_cache.hpp` | `Entry` with a `shared_ptr` image; `take()` returns the pointer; `set_on_change` and its five call sites (`:340`, `:413`, `:432`, `:453`, `:509`) |
| `include/strata/core/conversation_disk.hpp` | `Job.due/serial/cancelled`; the ordered queue; `Match` unchanged from phase 1 |
| `src/core/conversation_disk.cpp` | `put()` arms the due time and replaces a queued job (`:306`); `writer()` waits until the due time (`:341`); `write()` takes the "debounced" or the "forced" shape (`:367`); `wait_idle()`/`pending()` (`:517`, `:522`); the Windows priority |
| `src/program/generate.cpp` | `incoming` as a `shared_ptr` (`:14366`, `:14400`); the new flag (`:3267-3275`); `disk_writes` (`:10583`) is no longer the only gate |
| `strata_cli/launch/settings.py`, `serve/server.py`, `tests/terminal/test_launch.py` | flag plumbing |
| `docs/DETAILS.md:814-865` | the tier's section: the debounce, the flag, eviction and QUIT forcing the write |

## Tests

`conversation_disk_test` (CPU only), with a `before_write` hook already in the options
(`conversation_disk.hpp:33-34`) and short values (50-100 ms) for the debounce:

1. **debounce**: park, nothing written before the deadline, written after; `stats().written` is 0
   meanwhile.
2. **reset**: two parks 20 ms apart with a 100 ms debounce write once, with the second image.
3. **read cancels**: park, read the same entry before the deadline, no write happens; the file is
   not created.
4. **eviction bypasses**: with `flush = 3600`, evicting writes at once.
5. **QUIT bypasses**: with `flush = 3600`, `wait_idle()` writes everything and returns quickly.
6. **one state per debounced flush**: two debounced flushes at 200k and 260k tokens write one state
   each, and the second only because the growth passed 16 384.
7. **growth threshold**: a flush that adds 1 000 tokens writes K/V and no state.
8. **shared ownership**: the cache's `bytes()` does not change on a park; the image's `use_count` is
   2 and falls to 1 after the write; a park that is read back before its write leaves no file.
9. **take during a write**: the reader gets the image, the writer finishes, the bytes are freed once
   (no double free, no use after free).
10. **flag 0**: with `flush = 0`, a park writes nothing and an eviction writes the whole image —
    the phase 1 behaviour, so the regression is a test failure, not a measurement.

## Validation on hardware

Run on this PC's serving profiles on live agent traffic, 2026-11-05 to 2026-11-08 — the section
"Live days on the serving profiles" in
[CONVERSATION_DISK_STORE_PHASE2_STEPS.md](CONVERSATION_DISK_STORE_PHASE2_STEPS.md):

- bytes written per day, split by K/V and states: the debounced writes append their delta (54 of 97
  writes appended on 2026-11-05, 44 of 56 and 115 of 217 on the later debounced runs), and the median
  K/V past the deepest stored state is 2763-10 819 tokens against 127 397 in the flush-0 arm;
- the duration of the QUIT save: 3.1 s on the debounced day, a clean QUIT with 50 conversations
  (75 707 MiB) on the debounced restart of 2026-11-06, against flush-0 stops of 15.3-29.8 s and a
  full-store flush-0 day that did not fit the server's 300 s window;
- waits for the writer (the claim of this phase): they go down — 1 (2050 ms), 1 (5208 ms),
  7 (14 340 ms), 4 (6252 ms) across debounced runs, against 5 (16 092 ms) in the flush-0 arm;
- the park's own duration (median 234 ms, #2264): park medians 125.5-226.8 ms with the debounce,
  the worst debounced park 4698.5 ms against the flush-0 arm's 8515.0 ms;
- the `not read` count: 0 in every debounced run (8, 2373 tokens, in the flush-0 arm), so the
  "one state per flush" shape holds on this traffic so far;
- RAM hits are 0 on every live run, so the queued-image handback and the running-write wait stay
  proven offline only.

## Risks and open questions

- **The hot path.** `shared_ptr` in `ConversationCache` touches every park, take and admission. Any
  regression there costs more than the whole feature saves; the park duration is the gate.
- **Moving out of a shared image.** The restore path moves stage images and checkpoints; the
  cancel-on-take rule is what keeps the writer out of a gutted image. A review must check every
  `std::move` on the restore path (`generate.cpp:14366-14445`).
- **`pending()` semantics.** A deferred job is not RAM pressure. Getting this wrong turns the
  debounce into a periodic `wait_idle()` from `make_ram` — precisely the stall this phase removes.
- **Crash value is zero.** The store is a cache; a crash costs one longer prompt read. This is why
  `0` (off) is a defensible default and why the debounce can be long.
- **Static analysis, unconfirmed.** Everything above about the current code is read, not run, except
  the measurements quoted from CONVERSATION_DISK_STORE_MEASUREMENTS.md and `DETAILS.md`.
