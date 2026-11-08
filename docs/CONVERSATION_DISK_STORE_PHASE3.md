# [Engine] the disk tier, phase 3: one copy of a shared prefix

A design note, not a change: no code is touched in this document. Phase 3 depends on phase 1 (blocks)
and is worth little without phase 2 (frequent flushes)
[CONVERSATION_DISK_STORE_PHASE1.md](CONVERSATION_DISK_STORE_PHASE1.md),
[CONVERSATION_DISK_STORE_PHASE2.md](CONVERSATION_DISK_STORE_PHASE2.md). Agree the scope in an issue
before any code (CONTRIBUTING). This is the largest of the three phases and the one that can wait.

## The problem

After phase 1 and 2, each conversation appends its own blocks, so **every conversation that shares a
prefix with another stores it again**. An agent and its sub-agents share the system prompt and the
tool list; two chats of the same client share the whole system prompt.

Measured on this PC after the store was moved to the SSD (RTX 4080 16 GB, 48 GB RAM, Windows 10,
`--conversation-disk-mib 153600 --conversation-disk-persist on`), from the thirteen `.idx` files:

| | |
|---|---|
| entries on the disk | 13, 21 118 MiB, 1 299 224 tokens |
| shared prefix between entries | 9 372 tokens (the system prompt), 7 362-7 366, 7 196 |
| the large case | one growing conversation written again: 177 006, 178 725 and 196 984 tokens shared between pairs, ~2.3-2.5 GiB each |
| cost of the duplication | **14 429 MiB of 21 118 MiB (68.3%)**; on the HDD before the move, ~900 MiB of 6 755 MiB (13.3%) |
| the cache's tenth rule (#342) | cannot help: it needs at most a tenth of the entry's tokens unshared |

No entry was fully inside another (`fully inside another entry: 0 MiB`), so this is not a stale copy
of one session; it is the same prefix stored once per copy. Two shapes: the shared system prompt of
branches, and — the larger one — the same conversation written whole again as it grew, which phase 1's
append and supersede rule removes before phase 3 is needed. Phase 3 is for what remains: prefixes
shared between conversations that are not the same conversation.

## Scope

In:

- blocks become store-wide: one physical copy, referenced by every entry that holds that prefix;
- a block's identity includes its position in its conversation, so a block cannot be spliced into
  another conversation;
- reference counts, deletion at zero, garbage collection;
- a bounded window of state blocks per conversation (`--prompt-cache` of them plus the root).

Out:

- sharing the K/V of a *divergent* tail (only identical prefixes are shared);
- partial-file reads of a block (a block is the unit);
- any change to phases 1 and 2's triggers or format for the conversation's own index.

Not proposed here, and still out of reach: restoring from a prefix image of another conversation
without reading its file. The session code reads whole stage images; a cross-conversation restore
would need partial reads and a way to merge a prefix image with a tail. Phase 3 only stops storing
the same bytes twice.

## Design

### 1. Block identity

A block's key is everything that makes its bytes mean what they mean:

| part | why |
|---|---|
| model and config fingerprint (`SessionFileIdentity`) | another model or engine version makes the same tokens mean other K/V |
| geometry, layer range, `cvec` | the same reasons the session files refuse a mismatch (`conversation_file.cpp:750-760`) |
| `kind` | a `kv` block and a `state` block never mix |
| `prefix_digest` | the block's place in its conversation: a hash of every token and image below `tokens_start`, chained from the previous block |
| `tokens_start`, `tokens_end` | the range the bytes cover |
| `hash_lo`, `hash_hi` | the payload's own 128 bits, from the phase 1 table |

Two identical prefixes produce the same `prefix_digest` and the same payload hash, so they collapse
into one block. Two blocks with the same bytes at different positions keep different `prefix_digest`
values and stay apart — this is the cheap guard against splicing.

On a hash hit the store compares the **first and last 4 KiB** of the payload before sharing it.
A full compare of a 19 MiB block per hit is too expensive; the hash is already 128 bits and the data
is not adversarial (`conversation_file.hpp:29-30`).

### 2. Layout

```
<dir>/<identity-hex>/pack-00001.bin      append-only, capped at an absolute cap (8 GiB suggested)
<dir>/<identity-hex>/pack-00002.bin
<dir>/<identity-hex>.idx                 hash -> pack, offset, size, kind, range, refcount
```

One store per identity: a model or settings change produces a different namespace, and the old one is
removed by the start-up scan because no entry of the current identity names it.

The alternative — one file per block, `blocks/<xx>/<hash>` — is simpler for deletion (unlink at
refcount zero) but pays an open per block on every restore, which on Windows dominates for the
hundreds of blocks a long conversation reads. Packs are the recommendation; if compaction turns out
to be the hard part, per-block files are the fallback and this document says why they were not
chosen.

A conversation's `.idx` (phase 1) keeps its ordered list of block ids instead of offsets: the
conversation's own file disappears, its blocks live in the packs.

### 3. Reference counts and deletion

- Adding a block to an entry: hit → `refcount++`; miss → append to the current pack, write the store
  index, `refcount = 1`.
- Removing an entry (budget, history rewrite, `clear()` #879, `--conversation-disk-persist` off at
  exit): `refcount--` for each of its blocks; zero → free.
- A free block is dead space in its pack. Compaction rewrites a pack without the free blocks when
  the dead share passes a threshold (25% suggested), temp file + rename, and the store index is
  published after. Readers take a snapshot of the store index and a pack is retired only when no
  reader can still hold it: a pack is never deleted inside the run that created a reader for it
  except at exit, when nothing is reading.

### 4. The window of state blocks

Phases 1 and 2 accumulate states — one per flush, plus the chain at every eviction. Without a bound,
a long conversation accumulates 118 MiB per checkpoint for its whole life (a 300-turn agent
conversation would be 35 GiB).

Rule: keep the newest `--prompt-cache` (6) states per conversation, plus the shallowest one (the
root, `conv_cache.hpp:18-24`). Older states are dropped from the conversation's index at the next
flush; their blocks lose a reference and the store collects them.

This is the answer to "never drop an evicted checkpoint": the window keeps what a rewind can use, and
the root keeps what every branch resumes from. The measured use is the shared system prompt, which is
the root.

### 5. Crash consistency

Same order as phase 1: block bytes first, the store index second, the conversation index third. A
crash can leave a block no index names (dead bytes, collected by compaction) and never an index naming
bytes that are not there.

### 6. Removing the phase-1 crutch

A read asks for the payload of every block below the state it lands on, so a conversation whose states
are not bounded can ask for more RAM than the host has free, and its file is then never read (measured
2026-11-03: a 4897 MiB entry with 31 states refused at 5391 MiB free, `not restored (not enough RAM)`,
the prompt read in 67 s). Phase 1 carries the crutch for that: a read refused for RAM removes the entry
and its files by default; `ConversationDiskOptions::keep_on_ram_refusal` (off by default, set by the flag
`--conversation-disk-keep-on-ram-refusal`) keeps the entry instead. The stat `ram_drops` counts the
removals, the log line says `dropped N tokens on the RAM refusal`.

This phase deletes the crutch, not fixes it: with one state per flush (phase 2) and the window of the
newest states plus the root (section 4), a read asks for its K/V plus at most 7 states, so the preflight
fits the host on this PC (the same entry: 1406.9 MiB of K/V plus 788.2 MiB of states = 2195.1 MiB plus
the 512 MiB floor, against 5391 MiB free). What to take out in one change: the `ram_refused` branch in
`ConversationDiskTier::take`, the option, the flag and its help line, the `ram_drops` stat and its term
in the totals line, the store test's "ram drop" checks, and the paragraphs in
[CONVERSATION_DISK_STORE_PHASE1_STEPS.md](CONVERSATION_DISK_STORE_PHASE1_STEPS.md). Removing it leaves
the refusal as a plain read failure: the entry stays, the prompt is read, the bytes are not freed. A
refusal left standing after the window is a sign the window is too big, and the answer is the window,
not a deletion.

## Expected effect

From the measurement: 14 429 MiB of 21 118 MiB, mostly one growing conversation written again (the
phase 1 fix), and it grows with the number of branches —
every sub-agent adds the same prefix again on an unshared store. The test is the same day re-run
with the `.idx` reader that produced the numbers above, and the answer to "did the duplication
disappear" is the same tool, not a new one.

## Tests

`conversation_disk_test` (CPU only):

1. **one copy**: two conversations that share a prefix write one set of blocks; file bytes say so.
2. **reference counts**: drop one conversation, the blocks stay; drop both, the store index says
   free; a restart reads the same counts.
3. **position guard**: the same payload handed in at another `prefix_digest` is not shared.
4. **verify on hit**: a block whose first KiB differs from its hash is refused, not shared.
5. **a damaged block** affects both conversations, and the read error names the block; the fallback
   to an earlier state (phase 1's rule) still works.
6. **compaction**: a pack above the dead-space threshold is rewritten, live blocks keep working, a
   reader that started before it finishes is unaffected.
7. **window**: eight flushes leave six states plus the root, and the two dropped ones are collected.
8. **identity change**: a different model or config leaves the old store unreferenced, and the
   start-up scan removes it.

## Validation on hardware

Not run. The measured targets are CONVERSATION_DISK_STORE_MEASUREMENTS.md's: 14 429 MiB of 21 118 MiB duplicated, and on the old drive 21-26% shared per
write. The baseline moved, measured on this PC in the folder the live traffic serves: **at 20:31 on 2026-11-03** —
58 entries, 76 397 MiB, the duplicated K/V 4364 MiB = 12.0%; **at 00:53 on 2026-11-04** — 37 entries,
74 939 MiB, 18 892 MiB = 43.1% of the K/V, 25.2% of the store; **at 07:29 on 2026-11-04** — 35 entries,
76 155 MiB, **16 914 MiB = 36.3% of the K/V, 22.2% of the store**, the biggest pair sharing 197 120 tokens
of one dialogue. `0 superseded` in every one of the 403 writes of the phase-1 era.
The run: the same day of agent sessions with the same `.idx` reader, plus the phase 1 and 2
numbers (bytes per day split by K/V and states, QUIT duration, waits, restore time, `not read`).
Restore time is the number to watch: a pack is not the single sequential file of today, and a
restore of a 165K-token conversation was 5.6 s at 643 MB/s on the 2x CMP box
(`DETAILS.md:860-861`).

Part of this ran on 2026-11-06. The reader became a tool, `tools/conversation_store_scan.py` (the
ad-hoc scripts of the earlier snapshots are not in the repo), and the store the morning after the
phase 2 live day reads: 38 entries, 76 539 MiB, K/V 46 917 MiB, states 29 500 MiB, the duplicated
prefix **13 706 MiB = 29.2% of the K/V, 17.9% of the store** — the fourth snapshot of the series,
down from 36.3% and 22.2% on 2026-11-04, as the day's 54 appends merged growing conversations. The
restore time came from live traffic the same day: 43 disk hits, mean 982 MB/s, median 3.2 s, p95
5.2 s, max 6.9 s (154 871 tokens, 4598 MiB, 701 MB/s) — the same time band as the 2x CMP baseline
on this PC's SSD, before any pack sharing exists.

The scan of 2026-11-08, after three more debounced days of live traffic, answers the duplication
question: 64 entries, 76 624 MiB, the duplicated prefix **58 513 MiB = 76.4% of the store** (117.7%
of the K/V — a block is counted once for every pair that shares it), against 26 797 MiB of states
(35.0%). The debounced shape shows in the count: 49 entries of 64 hold one state, and the biggest
pair shares 201 113 tokens. The duplication did not go away; the delta appends grew it, and the
duplicated prefix is now the dominant part of the folder.

## Risks and open questions

- **Garbage collection is where caches leak.** Refcounts that are decremented twice, or never,
  either free live blocks or grow the store without bound. The window rule and compaction are the
  two places to review hardest.
- **Restore locality.** Blocks of one conversation should be appended to a pack in order, so a
  restore is close to sequential. A store that appends by hash order would scatter it. The pack is
  the conversation's write path, not a hash table.
- **The identity namespace duplicates everything** on a settings change. Acceptable (it is a cache),
  but the start-up scan must remove the old namespace, and this is a test.
- **Complexity.** This phase is bigger than phases 1 and 2 together. It is justified only if the
  measurements after phase 2 still show the duplicated prefix as the dominant cost of the disk; if
  they do not, the honest answer is to stop at phase 2. Measured on this PC in the folder the live traffic
  serves: **at 20:31 on 2026-11-03** — 58 entries, 76 397 MiB, the duplicated prefix 4364 MiB (12.0% of the
  K/V, 5.7% of the store) against 40 083 MiB of states (52%); **at 00:53 on 2026-11-04** — 37 entries,
  74 939 MiB, the duplicated prefix 18 892 MiB (43.1% of the K/V, 25.2% of the store) against
  31 076 MiB of states (41.5%), the biggest pairs sharing 116 022-197 120 tokens of one dialogue;
  **at 07:29 on 2026-11-04** — 35 entries, 76 155 MiB, the duplicated prefix **16 914 MiB (36.3% of the
  K/V, 22.2% of the store)** against 29 612 MiB of states (38.9%); **on 2026-11-08** — 64 entries,
  76 624 MiB, the duplicated prefix **58 513 MiB (117.7% of the K/V, 76.4% of the store)** against
  26 797 MiB of states (35.0%) — a K/V block is counted once for every pair that shares it, so the
  K/V share passes 100%. The share moves with what the budget keeps, and the long-prefix copies are what
  phase 1 cannot stop (the chain rule sends a continuation to a new entry). After three debounced days
  of live traffic the duplicated prefix is the dominant cost of the disk, which is the measurement this
  phase's justification asks for. Phase 3 is judged beside phase 2's state window, not against the
  baseline's 68.3%.
- **Static analysis, unconfirmed.** The duplication numbers are measured by that file's `.idx`
  reader; everything else here is read, not run.
- **Conversations that compaction makes known-stale, to be researched separately.** When the client
  compacts a dialogue (rewrites its prefix), every stored copy of the pre-compaction branch — in the
  RAM cache and on the disk — can never be matched again: a request carries the compacted prefix, and
  the store's rule is that a read needs the stored image-key chain as a prefix, so a chain from before
  the compaction is not one. Today those bytes sit until the budget evicts them. The measured case is
  the 4897 MiB entry of section 6: its dialogue survived a compaction, the entry was deleted by hand,
  and the folder after is 68 indexes, 68 `.bin`, 70 692 MiB, 0 indexes naming a missing file, 0 files
  named by no index (`E:/strata-conv-data`, 2026-11-03). A rule that drops known-stale conversations
  from both tiers is worth having; what it needs is a separate investigation — where the compaction
  signal comes from, which copies it makes dead, how to prove a copy is dead before removing it. No
  design here.
- **Letting a disk read page to swap, to be researched separately.** Today the gate compares the read's
  `need` (the payload of every block below the state it lands on) plus `--conversation-cache-min-free-mib`
  with the host's free RAM (`make_ram`, `generate.cpp:10612`), and paging is not part of the model: a
  conversation whose payload is above what the host reports free is refused, and the eviction loop
  evicts parked conversations before anything pages. Measured on this PC 2026-11-03 with the floor at 0:
  the start scan `67 conversations, 69 965 MiB, 0 refused`, reads landed (`prompt 82 088 tokens = 82 053
  reused + 35 read in 2207 ms; reused from the disk in 1.7 s`), `0 dropped on RAM refusals`, `0 waits for
  RAM` — a floor of 0 admits a read whose need is below the free RAM, and no refusal appeared. The
  unmeasured half is the host paging other processes' pages out for a payload of several GiB: the price is
  page faults (a fresh 118 MiB vector costs ~40 ms at a save, `generate.cpp:10636`), and no restore of a
  multi-GiB conversation under paging has been measured. A rule for this (a floor that accepts paging, a
  measured restore budget, or reading the blocks in batches the way the prompt path does) needs its own
  investigation; no design here.
- **One store for several models, to be researched separately.** Today the store is one folder per
  preset: `--conversation-disk-dir` and `--conversation-disk-mib` belong to a model's launch, the index
  binds model, config and engine version (`SessionFileIdentity`), and the start scan refuses an index of
  another model, engine version or settings — measured on this PC with one model: 67 conversations,
  69 965 MiB under a 76 800 MiB budget, `0 refused` at the start, `1 removed for the budget` after a
  write (2026-11-03). The wish is one folder and one size limit shared by several models, with the
  budget removing the least recently used conversation across all of them, not one model evicting only
  its own. The rule exists inside one model already: the budget's order is `last_used_s` (a read stamps
  it), and the entry the budget removes next is the least recently used whole conversation
  (`conversation_disk.cpp:61, 76`). What the investigation has to answer: what two models share at all
  (the block key includes model, config and geometry, so two models share no bytes — a shared store is
  about the budget and eviction, not about sharing blocks), how the scan tells one store's namespaces
  apart, how `pinned()`, QUIT `persist` and the "budget only" line behave when several models write into
  one folder, and which log lines say what was evicted for whom. No design here.
