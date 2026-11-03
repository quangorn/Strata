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
write. The run: the same day of agent sessions with the same `.idx` reader, plus the phase 1 and 2
numbers (bytes per day split by K/V and states, QUIT duration, waits, restore time, `not read`).
Restore time is the number to watch: a pack is not the single sequential file of today, and a
restore of a 165K-token conversation was 5.6 s at 643 MB/s on the 2x CMP box
(`DETAILS.md:860-861`).

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
  they do not, the honest answer is to stop at phase 2.
- **Static analysis, unconfirmed.** The duplication numbers are measured by that file's `.idx`
  reader; everything else here is read, not run.
