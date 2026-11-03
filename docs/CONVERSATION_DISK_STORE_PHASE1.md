# [Engine] the disk tier, phase 1: an append-only block store

A design note, not a change: no code is touched in this document. Phase 1 of turning
`--conversation-disk-dir` from "one file per parked image" into a store.

**The series, one document per phase:**

| phase | document | what it buys |
|---|---|---|
| 1 | this one | append instead of rewrite; reads keep the file; a bad block costs a block |
| 2 | [CONVERSATION_DISK_STORE_PHASE2.md](CONVERSATION_DISK_STORE_PHASE2.md) | write at park with a debounce in the writer: eviction costs nothing, QUIT stops being a burst |
| 3 | [CONVERSATION_DISK_STORE_PHASE3.md](CONVERSATION_DISK_STORE_PHASE3.md) | one copy of a shared prefix on the disk; a bounded window of states |

Each phase ships on its own and is useful without the next. Agree the scope in an issue before any
code (CONTRIBUTING).

Where this comes from: [CONVERSATION_DISK_STORE_MEASUREMENTS.md](CONVERSATION_DISK_STORE_MEASUREMENTS.md)
measured one day of agent sessions on this PC and found the costs the current tier pays on every
eviction and every read. Numbers quoted below are that file's, measured on an RTX 4080 16 GB with
48 GB of host RAM, Windows 10, engine 0.1.55, Swift-Qwen3.8-Flash-Next-GS-RCO-IQ2_XS.

## The problem this phase fixes

1. **Every eviction and every park rewrites the whole image.** `ConversationDiskTier::write()`
   (`src/core/conversation_disk.cpp:367`) writes the live state, every checkpoint and the whole K/V
   as one payload. Measured on the SSD the tier now uses: 47 writes, 6 097 745 tokens, 88 306 MiB,
   91.7 s, mean 1 009 MB/s — 1 878 MiB per write, of which a turn is a few thousand tokens.
2. **Nothing appends, and nothing removes the shorter copy.** Every write is a new entry
   (`put()` gives it a fresh id, `:323`), and `best()` always takes the longest prefix
   (`conversation_disk.hpp:75-91`), so the shorter copy of a growing conversation is never read
   again. On the measured store that is **14 429 MiB of 21 118 MiB, 68.3%**, and the largest pairs
   share 177 006, 178 725 and 196 984 tokens: one conversation written whole, again and again.
3. **A read deletes the file.** `take()` removes the entry and its files
   (`src/core/conversation_disk.cpp:475`, `:495`), so the next eviction of the same conversation
   writes it again from scratch, and a client that switches back and forth pays a full write per
   switch. `docs/DETAILS.md:829-830` states the deletion as intended behaviour; it is the behaviour
   this phase removes.
4. **A file is the read unit, and one corrupt byte loses it.** The payload carries one hash for all
   of it (`src/core/conversation_file.cpp:1232`) and its length is checked against the file
   (`:1211`), so a restore of 7 362 more tokens reads a 2 395 MiB file (measured: 3 of the 4
   `not read` skips have exactly this shape), and a damaged byte costs the whole conversation.
5. **`pinned` and the LRU stamp do not survive a restart.** `used` sits inside the payload
   (`src/core/conversation_file.cpp:679-684`), and `pinned` is not written at all
   (`conversation_cache.hpp:34-35`), so a persistent tier cannot keep the rule "never drop a pinned
   shared prefix" (`conv_cache.hpp:57-78`) and cannot order entries by real use.

## Scope

In:

- a second container for **this tier only**: a header, then immutable blocks appended to the end;
- a block table with per-block offset, size, hash, kind and token range, and a chained prefix digest;
- **K/V in blocks from the start**, one block per page-aligned token range: appending a turn instead
  of a conversation, and reading only the blocks a request needs;
- `used` and `pinned` moved from the payload into the index;
- the live state stored as a state block at its position: no separate "live" section anywhere;
- reads that keep the file, and read only the blocks they need;
- the budget evicting whole conversations, least recently used first.

Out (later phases, named at the end):

- writing at park time with an idle debounce, shared ownership of the image by the cache and the
  writer, `shared_ptr` entries in `ConversationCache`;
- deduplication across conversations, reference counts, garbage collection;
- any change to the session files of the HTTP API (`/slots/0?action=save|restore`);
- the other backends: the tier's source is in the SYCL and Metal build lists (`sycl/CMakeLists.txt:258`,
  `metal/CMakeLists.txt:102`) and must keep compiling there; HIP, SYCL and Metal are not built and not run
  with the tier, and this fork builds the engine on this PC for this card only (CUDA) — see the tier's
  section in [DETAILS.md](DETAILS.md).

The default path stays byte-identical: the tier is already opt-in through `--conversation-disk-dir`,
and its files are named `strata-conv-*`, a namespace nothing else uses
(`conversation_disk.cpp:55-59`).

## Why this order

Append alone removes the rewrite: after the first flush of a conversation, an eviction writes only
the blocks that are new. That is the largest single win of the whole plan and it needs no new trigger
and no new thread — `put()` keeps coming from `set_on_evict` (`src/program/generate.cpp:10585`) and
from the QUIT path (`:17789-17797`). Write-through (phase 2) only decides *when* to call `put()`; it
is worth doing after the store can absorb frequent calls cheaply. The block format is paid once,
here: phase 2 adds no new container, and phase 3 only adds identity and reference counts on top of
the same table. A K/V block per flush instead of per token range would have to be redone in phase 3.

## The data model

| term | meaning |
|---|---|
| entry | one conversation, one `.idx` and one `.bin` per stage |
| block | an immutable byte range in a `.bin`, described by one record in the table |
| `kv` block | the K/V of one page-aligned token range `[start, end)` for every layer of this stage |
| `state` block | one checkpoint's state (`gdn`, `ple`, `tails`, `dead`, `block_pos`) with its ids and images |
| end state | the running state at the end of the conversation. It is a `state` block like any other; "live" exists only in RAM |
| `position` | the token count a `state` block's snapshot is taken at |

Every block is immutable. The only way a conversation's bytes change is a new entry, and the only
way bytes leave the disk is the whole entry going.

## Container v2

Two files per conversation per stage, replacing today's `.bin` + `.idx` pair:
`strata-conv-<nonce>-<id>-<stage>.bin` and `strata-conv-<nonce>-<id>.idx`.

### `.bin`

64-byte header, then blocks, appended in write order. No payload length and no trailer: integrity
belongs to the block table in the index, not to the file.

| offset | field | type |
|---|---|---|
| 0 | magic `"STRACNV2"` | `u64` |
| 8 | version = 2 | `u32` |
| 12 | header size = 64 | `u32` |
| 16 | model fingerprint | `u64` |
| 24 | config fingerprint | `u64` |
| 32 | entry id | `u64` |
| 40 | stage | `u32` |
| 44 | reserved, 0 | `u32` |
| 48 | reserved, 0 | `u64[2]` |

`model` and `config` are `SessionFileIdentity` (`conversation_file.hpp:25-27`), the same binding the
session files use: a file of another model, engine version or settings is refused before anything is
read.

### `.idx`

Native-endian, counts before arrays, magic `"STRCIDX2"` (today `"STRCIDX1"`,
`conversation_disk.cpp:68`). One small file, rewritten atomically (temp + `fs::rename`,
`conversation_disk.cpp:186-195`) after the blocks it names are on disk.

Entry level:

| field | note |
|---|---|
| magic, model, config | identity |
| `written_s`, `last_used_s` | `last_used_s` is new: the LRU order the budget uses |
| `cvec` | the control-vector state |
| `geometry[18]`, `layer_lo`, `layer_hi` | validated against the runtime as today |
| `tokens` | the deepest `state` block's position: what `best()` may claim |
| block count, then the table | below |

Block record:

| field | note |
|---|---|
| `offset`, `size` | `u64`; `offset + size` must stay inside the `.bin` |
| `hash_lo`, `hash_hi` | 128 bits: `SessionHasher` with two seeds (`conversation_file.hpp:31-43`) |
| `kind` | 1 = `kv`, 2 = `state` |
| `tokens_start`, `tokens_end` | for `kv`: the range; for `state`: both equal `position` |
| `prefix_digest` | `u64`: a digest of every token and image below `tokens_start`, chained from the previous block. It binds a block to its place in this conversation |
| (state only) `used`, `pinned` | the LRU stamp and the shared-prefix pin, moved out of the payload |

The start-up scan (`conversation_disk.cpp:231-262`) already removes whatever it cannot take in. A
v1 file fails the magic check and is removed, which is right: it is a cache, and the worst case is
one longer prompt read.

### Integrity

- A block is verified against `hash_lo`/`hash_hi` when it is read.
- `size` and `offset` verify the file was not truncated or extended behind the index: the `.bin`
  must be at least `offset + size` of the last block. A longer file is fine (a torn append); a
  shorter one means the index outlived its data and the entry is refused.
- A torn tail — a write that ended before the index was published — is not named by the table and
  is cut back to the last indexed block at start, or simply ignored by readers until the next append
  overwrites it (an append always starts at the end of the last *indexed* block).
- A damaged block costs that block and everything after it in the chain; the entry falls back to
  the deepest `state` block whose whole chain verifies. Today the same byte costs the whole
  conversation.

### Page alignment (the gate this phase stands on)

Phase 1 stores K/V in blocks from the start: one block per page-aligned token range, appended.
A block per flush would leave the phase's main win — appending a turn instead of a conversation, and
reading only the blocks a request needs — on the table, and phase 3 would have to redo the format.

`cells` is rounded up to `page_size` and the engine itself reuses K/V only in whole pages
(`conversation_snapshot.cpp:47`, `:302`), so `page_size` is the natural boundary: a `kv` block starts
and ends on a multiple of `page_size` cells, `kBlockTokens` is rounded down to a multiple of it, and
the indexer's `pooled` rows are sliced by the same boundary as `k`, `v`, `k_scale` and `v_scale`.

The first requirement of this phase is a golden test that proves, for every K/V `format` the engine
can produce for this model, that a page-aligned range of buffer bytes read from a block equals the
same range of a full write. If some format turns out not to be prefix-safe, the phase stops there and
the boundary is fixed in the K/V layout — not by falling back to whole-image blocks, which would
leave the format rewritten in phase 3 anyway.

## Behaviour

### Write path

`put()` and the writer thread keep their shape: one queue, one writer, FIFO
(`conversation_disk.cpp:306-365`). What changes is `write()` (`:367`).

For a job, in order:

1. **Find the entry to append to.** Not "the entry I wrote last time": the store outlives the run.
   Scan the entries and take the one whose chain is a prefix of the new image — same `cvec`, its
   deepest stored state's tokens and images match the image at that length, and every stored
   `prefix_digest` matches — preferring the longest. That is the entry a read would have picked
   (`best()`), so it is the one worth extending.
2. **Append, or start a new entry.** If a target was found, append. If none was found — the client
   edited, rewound or replaced history — write a new entry. Either way, **remove the entries the new
   image supersedes**: every entry whose whole chain is a prefix of the new image and shorter than
   it. That is the 68% of the measurement: the shorter copies are never read (`best()` takes the
   longest) and never removed by a 150 GiB budget. The removal is published with the new index, so a
   crash before it leaves both copies rather than neither.
3. **Append the `kv` blocks** for the tokens beyond the last stored `kv` block, in pieces of at most
   `kBlockTokens = 2048`, rounded down to a multiple of `page_size` (about 27-31 MiB at the
   measured 13-15 KB/token, close to the files' 16 MiB I/O block, `conversation_file.cpp:83`).
4. **Append the `state` blocks** the entry does not hold yet: every checkpoint of the image that has
   no block at that position, plus the end state (`image.live` at its position). This is at most
   `--prompt-cache` + 1 blocks, about today's 826 MiB at 118 MiB each — phase 1 deliberately keeps
   today's set, because flushes stay rare until phase 2.
5. **Publish the index.** Data first, index second, as today (`conversation_disk.cpp:406`), so a
   crash leaves blocks no index names, never an index naming bytes that are not there.
6. `durable = false` stays (`:386`): a cache.

The entry's `tokens` becomes the deepest `state` block's position. K/V beyond it is not written in
phase 1: it is only usable once a state covers it, and writing it early is what makes K/V outlive
its usefulness.

### Read path

`take()` (`:438`) stops deleting. It becomes:

1. Under the lock, find the entry and **copy** its table (a few hundred bytes) instead of removing
   it; take the reader's `last_used_s` bump.
2. Pick the deepest `state` block at or below the matched prefix; read its chain (`kv` blocks up to
   its position, then the state block itself).
3. Verify every block hash and the chain; on failure fall back to the previous state block, and
   only when none verifies return an error.
4. Return the image with the state as its `live` and its checkpoints reconstructed from the `state`
   blocks. `holds()` (`:42-53`) validates the keys against the index before any bytes are trusted,
   as today.

The entry stays on the disk. The next eviction of the same conversation appends its delta.

Two consequences to keep in mind when reviewing:

- `best()` (`conversation_disk.hpp:75-91`) reports `tokens` from the index, but a restore lands on a
  `state` block's position, which is at most one checkpoint interval behind. `Match` gains
  `restore_tokens`, and the "read the file or read the tokens" estimate
  (`src/program/generate.cpp:14370-14384`) must use it; otherwise it will claim a longer prefix than
  the file can give and read a file for nothing.
- With no deletion, two entries can hold the same conversation until the next flush removes the
  shorter one (write path, step 2). The window is bounded by that flush, not by a read.
  goes when the new index is published (rule 1 of the write path), so this is bounded.

### Budget

Unchanged in size (`--conversation-disk-mib`, default 32768) and in what it counts — the bytes of
the files — but the unit of eviction is now the **whole conversation**, chosen by `last_used_s`
(oldest first, `conversation_disk.cpp:373`). Individual blocks are never dropped in phase 1. In the
measurement's configuration (153 600 MiB, 21 118 MiB used) nothing is evicted; with the default the
policy now matches real use instead of write order.

`last_used_s` is bumped on a read and written with the next index publish (a read does not rewrite
the index immediately: a small, bounded loss of LRU accuracy after a crash is acceptable for a
cache).

### Start-up

`scan_folder()` (`:231`) keeps its shape: read every `.idx`, validate the identity and the table
against the `.bin` sizes, take in what passes, remove the rest, then apply the budget. New rules,
all one line each:

- an entry with no `state` block (a crash between the first K/V append and the first state append) is
  removed: there is nothing to restore from it;
- a `.bin` shorter than the table says, or a header of another entry id or stage, is removed;
- a `.bin` longer than the last indexed block is kept and the excess is ignored until the next
  append.

## Code map

| file | what changes |
|---|---|
| `include/strata/core/conversation_disk.hpp` | `Entry` gains the block table, `last_used_s`, and drops the split `live`/`checkpoints` byte payload (the keys stay); `Match` gains `restore_tokens`; `Job` unchanged in phase 1 |
| `src/core/conversation_disk.cpp` | `write_index`/`read_index` for v2 (`:164`, `:205`); `write()` append-or-new (`:367`); `take()` keep-on-read and block reads (`:438`); `remove_locked` unchanged (`:297`); `scan_folder` rules (`:231`) |
| `include/strata/core/conversation_file.hpp` | the block writer/reader helpers for the payload encodings the container reuses (`put_checkpoint`/`get_checkpoint`, `conversation_file.cpp:679`, `:748`), and the removal of `used` from that payload |
| `src/core/conversation_file.cpp` | `put_checkpoint`/`get_checkpoint` lose `used`; the slot-save format keeps it in its own place, so the API's files stay v1 and byte-identical |
| `src/program/generate.cpp` | the restore estimate uses `restore_tokens` (`:14370-14384`); nothing else moves in phase 1 |
| `src/core/conversation_disk_test.cpp` | the tests below |
| `docs/DETAILS.md:814-865` | the tier's section: the store, keep-on-read, the budget by use |

## Tests

`conversation_disk_test` (CPU only, `-DSTRATA_BUILD_CONVERSATION_TESTS=ON`), no GPU:

1. **golden: page-aligned prefix.** Write a full image, read back a range of a `kv` block, compare
   byte for byte with the same range of a full write — for every K/V `format` the model produces
   (the QSA layers, the draft layer, and the pooled indexer rows). This test is the gate: it runs
   before the rest of the phase is written.
2. **append.** Evict, flush, park and evict a longer image: the `.bin` grew by the delta only (the
   block table's sizes say so), and the restored image equals the one written in one go.
3. **keep-on-read.** Read the same entry twice: both reads succeed, the files are still there, the
   budget sees the entry once.
4. **budget by use.** Three entries, read the oldest, add a fourth: the second-oldest goes, not the
   one just read.
5. **torn tail.** Truncate the `.bin` past the last indexed block: the entry is taken in and the
   extra bytes ignored; truncate into a block: the entry is refused and removed.
6. **one bad block.** Corrupt one byte in the middle `kv` block: the read falls back to the deepest
   state whose chain verifies, and the error line says which block.
7. **`used` and `pinned` survive.** With `persist on`, stop and start: the checkpoints carry their
   `used` and `pinned` values, and the pinned one is not the budget's first victim.
8. **no state block.** Publish an index with only `kv` blocks: the entry is removed at start.
9. **append target across runs.** Write an entry, restart the tier, write a longer image of the
   same conversation: it appends to that entry and removes nothing; then write one that leaves a
   shorter entry fully inside it: the shorter entry's files are gone after the publish.
10. **history rewrite.** Park a conversation, then park one whose tokens diverge: a new entry is
   written and the old one is gone, with no orphan file.
11. **v1 refusal.** A `strcidx1` index and its files are removed at start without touching an entry
    of v2.

## Validation on hardware

Part of it is measured on this PC; the comparison is not run. Measured on two serving profiles with real
agent traffic, 2026-11-03/04 — the numbers, the launch lines and the log lines are in
[CONVERSATION_DISK_STORE_PHASE1_STEPS.md](CONVERSATION_DISK_STORE_PHASE1_STEPS.md), "Live traffic on the
serving profile":

- bytes written, split into new blocks and whole images: 32 appends of 403 writes (7.9%), 323 955 MiB of
  deltas against 383 782 MiB of published images — **59 825 MiB of rewrite avoided**; the density of an
  append is 4.39 KB/token, of a new entry 9.82, the baseline day 15.2. On the live run alone the append
  share is 9.1% of 77 writes;
- bytes read, entries and blocks on disk: 102 disk hits, 58 146 MiB at 546 MB/s, 3 662 976 prompt tokens
  given back over 12.9 h; the folder holds 35 conversations, 76 155 MiB (K/V 46 543, states 29 612), and the
  K/V duplicated across shared token prefixes is 16 914 MiB = 36.3% of the K/V (12.0% at the 20:31 snapshot
  of the same run, 43.1% at 00:53, against the baseline's 68.3%);
- a read's scope: the bytes read equal exactly the blocks below the state it lands on (34 reads matched to
  their entries), 87-100% of the entry;
- the `not read` count (7 refusals in 12.9 h, 187 216 prompt tokens read from 0 after them; 1 refusal in the
  smaller-cache run) and the budget's removals by use (103); 0 RAM hits, 0 dropped on RAM refusals,
  0 failed writes/reads, 0 fallbacks; 12 waits for RAM in the live run (50.7 s), one `waited 15486 ms for
  3 queued writes (their RAM was needed)` in the smaller-cache run;
- the RAM side, from the same logs: 88 superseded copies dropped from the cache in the live run, and 2609 of
  its 2738 parks reused the retained K/V allocation (42.6% of the era's snapshot bytes reused);
- start-up with a full folder: 68 conversations, 75 985 MiB (and 67, 69 965 MiB on the smaller-cache
  profile), `0 refused`, `0 stale files removed`; the index rewrite a publish costs is 2.4 MiB at 88 948
  tokens, 5.1 MiB at 231 677, 15.3 MiB at 163 466, 3.8 MiB at 165 807;
- the Windows build: every engine from `0.1.55.3` to `0.1.55.12` is built on this PC and has served.

Not run, and no saving claimed: the same day of agent sessions against a same-day run without the change;
a read of a damaged block on hardware (`0 reads fell back on damage` in every run); a read of a queued
image (0 RAM hits over all the runs, one `after X ms waiting for its write` in the scripted phase 11); the
disk pin in use (the folder has 0 pinned state blocks); a write refused by the free-space rule; the
crutch's drop line (0 dropped so far, and 0 `forced write, the whole image` lines in the live run); the
time of the start scan. The write line's MiB and MB/s count the whole published image, not the bytes
appended, and the `not read` estimate counts the whole entry, not the blocks a read needs — both recorded
as phase 2 step 9.

## Risks and open questions

- **K/V prefix safety.** The whole append design rests on the buffers being cell-major and
  prefix-safe at page boundaries, and there is no whole-image-block fallback: if a format is not
  prefix-safe, the boundary is fixed in the K/V layout before the store is written, because phase 3
  would otherwise rewrite the format anyway. Test 1 is the gate.
- **Index size.** A 300 000-token conversation keeps full ids per state: about 8 MB with 7 states.
  Rewriting that on every append is a few tens of milliseconds; at start it is one read. No digest
  short-cut is proposed, because `best()` needs the ids to compute the match length. Measured on this PC
  2026-11-03: the rewrite follows the **number of states**, not the token count — 2.4 MiB for 88 948
  tokens with 7 states, 5.1 MiB for 231 677 tokens with 7 states, 15.3 MiB for 163 466 tokens with 31
  states (a day of writes at a 16 384-token checkpoint interval). The time the scan spends on a full
  folder is not measured.
- **No compaction in phase 1.** Files only grow with the conversation's token count, so there is no
  garbage to collect — until phase 2 makes flushes frequent and phase 3 shares blocks between
  conversations.
- **`last_used_s` and the LRU across restarts.** Written with the next publish; a crash loses the
  order since the last write, which the budget tolerates.
- **Static analysis, unconfirmed.** The paragraphs above about the code are read, not run. What has been
  measured on this PC is in "Validation on hardware" above and in the steps file's "Live traffic on the
  serving profile"; the estimate that the `not read` rule counts the whole entry
  (`conversation_disk.cpp:1592-1598`, `generate.cpp:14424`) is read from the code, and the live refusals
  quoted 1600-2116 MiB for 7539-token reads whose entries are no longer in the folder.

## What phases 2 and 3 add on top (one line each)

- **Phase 2** calls `put()` at park time instead of eviction time, shares the image between the
  cache and the writer instead of copying it, and debounces the write in the writer. This document's
  format does not change: the due time lives in the writer, and a debounced flush writes the end
  state only.
- **Phase 3** stops storing the same block twice: the table's `prefix_digest` becomes the store's
  key, blocks get reference counts, and a bounded window of state blocks per conversation replaces
  "keep every state forever".
