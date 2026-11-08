# [Engine] the disk tier: the state window at write time, and a read of 7 states

A design note, not a change: no code is touched in this document. It fixes two measured defects of the
conversation disk tier (`conversation_disk.cpp`, engine 0.1.58-local) and states how it relates to
[CONVERSATION_DISK_STORE_PHASE3.md](CONVERSATION_DISK_STORE_PHASE3.md), which is designed but not done.

Measured on this PC, 2026-11-08: Qwen3.8-Flash-Next `IQ2_XS low_ram`, `--max-context 262144 --kv q4_0
--kv-resident 32768 --conversation-cache-mib 5120 --conversation-cache-slots 8
--conversation-cache-min-free-mib 0 --conversation-disk-dir E:\strata-conv-data --conversation-disk-mib 76800
--conversation-disk-persist on --conversation-disk-flush-seconds 60`.

## The problem

Two independent defects: states accumulate at write time, and a read asks for all of them.

**States accumulate from the number of writes, not from the context.** In `write()`, the `job.forced` branch
(eviction from the RAM cache, and QUIT) writes the live state unconditionally and adds the image's whole
checkpoint chain. Read from the `.idx` files (the v2 index, `index_v2_bytes`):

| file | tokens | K/V | state blocks | states | file |
|---|---|---|---|---|---|
| `d5c6ed4baa7362d0-317` | 141 590 | 1.19 GiB | 31 | 3.41 GiB | 4.60 GiB |
| `d5c6ed4baa7362d0-84` | 166 959 | 1.40 GiB | 25 | 2.75 GiB | 4.15 GiB |
| `d5c6ed4baa7362d0-407` | 155 609 | 1.31 GiB | 21 | 2.31 GiB | 3.62 GiB |
| `1d3e04cb7ccf8475-1` | 226 930 | 1.91 GiB | 15 | 1.65 GiB | 3.56 GiB |

One state block is 112.6 MiB whatever the conversation length; K/V is about 9 KB per token. The entry at
141 590 tokens holds 31 states whose positions after ~127k sit 25-800 tokens apart, while the entry at
226 930 tokens holds 15: it was evicted less often. Fewer tokens, bigger file.

**A read asks for every state.** `state_positions()` returns every position not deeper than the landing
point, `read_bytes_of()` prices the K/V below it plus **every** state block at or below it, and
`read_entry_image()` reads them all. `Match::bytes`, the "read the file or read the tokens" estimate and the
`admit()` preflight all see 4.6-6.5 GiB.

Measured consequences, live log 2026-11-08 14:00-14:20:

- a park is refused for RAM by 20-300 MiB (`skip parking (physical RAM admission; need 941 MiB plus 0 MiB
  floor, 914 MiB available)`), so the conversation reaches neither the RAM cache nor, on the way back, the
  disk;
- a disk read asks 6278 MiB for 114k tokens and 6527 MiB for 115k, with 6310 MiB free, and is refused;
- on the refusal the entry and its files are removed (`dropped 115053 tokens on the RAM refusal: 6527 MiB of
  files are gone`), because `keep_on_ram_refusal` is off by default.

## Rules

`W` is `--prompt-cache-every` (default 16 384); there is no separate setting for the disk. `W = 0` means no
gap requirement: a state is written at every write.

1. Adjacent states in a file are at least `W` apart, with one exception: the root (next rule). The gap rule
   holds for every write, QUIT and eviction from the RAM cache included, so those writes may put fewer than 7
   states in the file.
2. The root is pinned: the shallowest state of the write - the file's shallowest, or the image's, whichever is
   lower - is always written and never dropped, even when the state above it is closer than `W`. This is the
   point every request shares, in practice the end of the system prompt, and the RAM cache pins it for the same
   reason (`conv_cache.hpp:18-24`). A checkpoint the image marks `pinned` (the shared-prefix pin) is protected
   the same way. The root counts as one of the 7.
3. If the live state is less than `W` past the newest state in the file, nothing is written at all: no state,
   no K/V, no index. The same rule covers the background (idle) flush.
4. The live state is no exception to the gap or the window: it is not protected the way the root is, it simply
   is the newest point.
5. The target is 7 states in a file, live and root included. More are not added; fewer are allowed when the gap
   does not permit more. States already in the file are never removed: the file is append-only and there is no
   collection of old blocks (decided).
6. The image's checkpoint chain is never written. A write adds either one live state (append branch) or a set
   of up to 7 (fill branch).
7. K/V is not written above the deepest state being written: a read cannot use it. In practice K/V goes up to
   `tokens` when the live state is written, and no K/V is written at all when the write is skipped.
8. A read takes the landing state plus at most 6 states below it (7 in all). The RAM need, the "file or
   tokens" estimate and the `admit()` preflight are computed from that set.
9. Files written before this change (up to 31 states) are not rewritten: a read of them also takes 7 states,
   but the extra blocks stay. They shrink only when a file is recreated (for example when the identity
   changes). Their root is their shallowest state, whatever it is.

## The write algorithm

The branch depends on how many state blocks the file already holds in every stage at once (the same check
`state_positions()` makes).

- **Fill**: no file, or fewer than 7 states in it. Candidates are the file's positions, the live state and the
  image's checkpoints.
- **Append**: 7 or more states in the file. One candidate: the live state.
- **Idle flush**: one candidate, the live state, whatever the file holds.

Then one pipeline of two stages.

**Stage 1, the gap.** The root (the shallowest position of the union, and any position the image marks
`pinned`) is written unconditionally: it is never cut by the gap, however close the state above it is.
Candidates are then ordered newest first. The live state is taken first if it is at least `W` past the newest
state in the file. Then the walk goes down and takes a candidate only when it is at least `W` from the nearest
taken one (or from a state already in the file). Positions already in the file are immovable: they are
barriers, never removed. The result has adjacent positions at least `W` apart, except for one possible pair:
the root and the state just above it. With `W = 0` the gap check is skipped and everything is a candidate.

**Stage 2, thin to 7.** If more than 7 were taken, the extra ones are dropped with the existing
`spread_victim()` (`include/strata/program/conv_cache.hpp:102-119`), the one the RAM cache uses for
`--prompt-cache-spread`: it drops the point with the smallest relative gap and never drops the root or the
newest point. The list is given oldest first with the live state last; the root, the image's `pinned`
checkpoints and the positions already in the file are marked `pinned` so the thinning does not choose them.
Since `spread_victim()` only removes points, the gap property survives. The root being index 0, it is never a
victim, and it takes one of the 7 places.

Order of calls: `states_in_file()` then `gap_thin()` then `spread_victim()`, then the blocks for the accepted
positions the file does not hold yet.

If nothing is left to add (the live state was cut by the gap and every other candidate is already in the
file), no write happens: the file and its index are untouched and `stats().written` does not move. The id
mapping of the queued job must still be recorded under the mutex (`published_`), or a read that matched the
unwritten image will report "the entry is gone" afterwards.

## Where the code changes

`src/core/conversation_disk.cpp`:

- `write()` (`:1439-1516`): the fill / append / idle branch above replaces `write_state` and `states[s]`;
  `job.forced` stops being an exception; `plans[s].limit` is capped at the deepest state written;
- new helpers beside `state_positions()` (`:823`): `states_in_file()`, `gap_thin()`, and the assembly of the
  positions to write;
- `merge_keys()` and `merge_live_key()` (`:786-819`) become one function taking the accepted positions: the
  entry's keys plus those positions. The image's chain is no longer merged;
- the log (`:1705-1709`): "the whole image" goes; three lines take its place - what the fill branch added,
  that one end state was written, or that the write was skipped and why (how many tokens past the newest
  stored state, against `W`).

The read path (`:823`, `:848`, `:864`, `:983`): `state_positions()` stays; its callers truncate the result to
7. `read_bytes_of()` takes the chosen set and prices only it. `read_entry_image()` builds `below` from at most
6 positions and reads a state block only from the set. `conversation_disk_read_bytes()` uses the same set, so
`Match::bytes`, the "file or tokens" estimate and the preflight agree. The `prefix_digest` chain is recomputed
for every block without reading, so skipping states does not break it.

`include/strata/program/conv_cache.hpp`: `gap_thin()` lives beside `eviction_victim()` and `spread_victim()`:
it is a pure helper, independent of the engine, and tested on its own. It takes the positions oldest first,
the window `W`, and the flags of the points that may not be cut (the root at index 0 - the same rule
`eviction_victim()` applies - and the image's `pinned` checkpoints); it returns the positions to write.

`include/strata/core/conversation_disk.hpp`: a constant for the target of 7 states with its reasoning; the
comments of `prompt_cache_every` (it says "a debounced flush" today), `put()` ("Every checkpoint is written
with it" stops being true), `Match::bytes` and `take()`.

`src/program/conv_cache_test.cpp`: tests for `gap_thin()`. `src/core/conversation_disk_test.cpp`: the tests
below.

## Tests

Most of the existing `conversation_disk_test` blocks survive: the helper `image()` builds 3 states (live and
two checkpoints), and the fill branch accepts all of them for a short conversation at `W = 16 384`.

Implementation note (2026-11-08): at the real window those blocks would keep only the root and the end state of a
600-token image, and their numbers are about the store and the read path rather than the window, so their
`opts()` sets `prompt_cache_every = 0` - the documented no-gap mode, where a write keeps every candidate. The
window's own rules are the new "window" block and the rewritten "shape" block. Two details the note left open and
the code settles: an idle (debounced) flush offers the live state alone even on a new file (so the root is not
added there), and the thinning marks the newest point protected as well - `spread_victim()`'s fallback to
`eviction_victim()` would otherwise drop it when every other candidate is pinned.

- "store" (`:916-990`): `states == 6` and `back.checkpoints.size() == 5` do not change;
- "the cache's events drive the disk tier" (`:540-543`) and "pinned quit" (`:665-673`): only the asserted log
  line changes; the file's header (`:8`, `:17`) describes the old forced shape and is rewritten.

To add:

1. **fill to 7**: an image with 10 checkpoints on a new file leaves exactly 7 states, at the positions a
   hand-run of gap then `spread_victim()` gives;
2. **the root survives the gap**: candidates at 9 000 (the root), 20 000, 40 000 and a live state at 60 000,
   with `W = 16 384`: the file gets all four, including the root and the 20 000 state only 11 000 apart, and
   the other pairs stay at least `W` apart;
3. **the gap beats the target**: the same image with a `W` above half the conversation leaves fewer than 7
   states besides the root, and any two adjacent ones other than the root pair are at least `W` apart;
4. **append under the window**: 7 states in the file, growth of 1k at `W = 16 384`: no write, the file is
   byte for byte the same, the log says so, and a read still lands on the deepest state;
5. **append over the window**: growth of 20k: exactly one new state, and K/V only up to it;
6. **window 0**: every write adds a state;
7. **idle**: with the live state closer than `W`, the background flush writes nothing even into a file of 2
   states;
8. **read with 15 states**: 7 are taken (the landing state plus 6 below), and `Match::bytes` is their sum plus
   the K/V;
9. **stages**: the accepted positions are written in every stage, and a read requires a block in every stage.

## Documentation

- `docs/DETAILS.md:884-899`: the shape of a write (three branches, the gap, "nothing written") and the cost of
  a read (7 states, not the whole file);
- `docs/CONVERSATION_DISK_STORE_PHASE2_STEPS.md`: its description of the forced shape ("the whole image") is no
  longer true;
- `docs/CONVERSATION_DISK_STORE_PHASE3.md` §4, §6 and test 7: see the next section.

## Verification

Build and run without a GPU:

- `cmake -S . -B build-conv -G "Visual Studio 17 2022" -A x64 -DSTRATA_BUILD_CONVERSATION_TESTS=ON`
- `cmake --build build-conv --config Release --target conversation_disk_test conv_cache_test`
- `ctest -C Release -R "conversation_disk_test|conv_cache_test" --test-dir build-conv`

The engine: `python3 packaging/build_engine_win_local.py engine`. The model does not need stopping and the
engine's version string does not change (`AGENTS.md`), so the disk store survives the build; the new code is
taken at the next engine start.

Live check in `C:\Users\karam\AppData\Local\Strata\launch\iq2_xs-low_ram.engine.log`: after an eviction the
line says "1 new state" (or "7 new state" for a new file), "the whole image" is gone, and `disk hit:` for a
large conversation asks about 7 x 112.6 MiB plus its K/V instead of 4.6-6.5 GiB.

Measured 2026-11-08 on this PC, package `0.1.58.1-windows-x86_64-cuda-cuda13`, engine started 16:03. A legacy
file of 115 386 tokens read as **1781 MiB in 2.96 s (630 MB/s)** and came back - the old build priced the same
read at 6278-6527 MiB, refused it and removed the entry. Live writes: `idle flush write, the end state written;
35342 tokens past the deepest stored state` (one end state and the K/V up to it); `fill write, 4 states in the
window (the root included), 3 new` with **0 new kv** - rule 7, no K/V above the deepest state written;
`skipped 127993 tokens: 12607 tokens past the newest stored state and --prompt-cache-every is 16384; nothing
written (no state, no K/V, no index)` from real traffic; an idle flush on a new entry wrote one end state with
the K/V ending at 96856 for the live 96859, and a read that restored a parked conversation cancelled its
queued write before the deadline. "the whole image" never appears in the log of this run.

## Expected effect (estimate, not a measurement)

For `d5c6ed4baa7362d0-317` (141 590 tokens, 31 states):

| | today | after |
|---|---|---|
| states in the file | 31 | 7-9 (growing only by the 16 384 gap) |
| states read | 31 | 7 |
| the read | 4.60 GiB, ~5.5 s | ~2.0 GiB (0.79 states + 1.19 K/V), ~2.4 s |

The 115 053-token conversation dropped at 14:19 (6527 MiB, about 48 states) would ask about 1.1 GiB of K/V
plus 0.79 GiB of states, ~1.9 GiB against 6.3 GiB free, and would be read.

## What this note does not do

- It does not remove old state blocks and does not compact (decided: not needed);
- it does not remove the refusal on RAM (`keep_on_ram_refusal`) - that is phase 3's section 6;
- it does not rewrite files already on the disk; they shrink only when recreated;
- it adds no setting: the window is `--prompt-cache-every`.

## Relation to phase 3

Phase 3 ([CONVERSATION_DISK_STORE_PHASE3.md](CONVERSATION_DISK_STORE_PHASE3.md)) is a design note whose main
subject is one shared copy of a common prefix (block identity, refcounts, packs, compaction). This note touches
it in three places and leaves the rest alone.

**§4, "The window of state blocks" - partly superseded, still needed.** Phase 3 §4 keeps the newest
`--prompt-cache` (6) states plus the root, drops older ones from the conversation's index at the next flush,
and lets the store collect the blocks. This note keeps 7 states including live - the same count - but does it
at write time and by the gap rule, and never removes anything. So after this note:

- the cause phase 3 §4 names ("phases 1 and 2 accumulate states - one per flush, plus the chain at every
  eviction") is gone: a write adds at most seven states once (the fill), after that one state per `W` of
  growth;
- phase 3's stated worst case ("a 300-turn agent conversation would be 35 GiB") stops being true: the bound
  becomes conversation length divided by `W`, about 1.5 GiB at 226k tokens;
- what remains of §4 is trimming *historical* files and long-lived ones: a file still grows by one state per
  `W`, and nothing removes the ones above the window. That part is unchanged and still worth doing;
- one conflict, now settled in the root's favour: phase 3 protects the root (the shallowest state, the shared
  system prompt) unconditionally, because every branch resumes from it. This note does the same: the root is
  pinned in `gap_thin()` and never dropped by either stage, so a file may hold one pair of states closer than
  `W` - the root and the state just above it - and the root takes one of the 7 places. Phase 3 §4's root rule
  therefore stands as written.

**§6, "Removing the phase-1 crutch" - this note is its precondition.** §6 says the crutch (a read refused for
RAM removes the entry) is deleted because "with one state per flush and the window ... a read asks for its K/V
plus at most 7 states". That property is exactly this note's rule 7, so:

- §6 can be done after this note, and its numbers hold: 1406.9 MiB of K/V plus 788.2 MiB of states;
- nothing in this note removes the option, the flag, the `ram_drops` stat or the removal itself. Until §6 is
  done, a read refused for RAM still deletes the entry, and refusals become rare rather than impossible (a
  150k-token conversation's K/V of ~1.3 GiB plus 0.79 GiB of states still needs a floor the host may not
  have);
- §6's wording "one state per flush (phase 2)" becomes "one state per `W` of growth"; the numbers in it do not
  change.

**Format, identity and dedup - untouched.** This note changes no container, no block record and no key: the
v2 `.bin` and `.idx` stay as they are, `prefix_digest` keeps its meaning, and phase 3's §1 block identity and
§2 packs can be built on top unchanged. §3's refcounts and compaction gain nothing and lose nothing: the
blocks this note stops writing are simply not allocated.

**Phase 3's baselines and test 7.** Two consequences to keep in mind when phase 3 is done:

- the 2026-11-08 snapshot prices the store at 64 entries, 76 624 MiB, of which states are 26 797 MiB (35.0%).
  This note cuts state bytes (fewer states per conversation, K/V without a dead tail), so the duplication
  percentages of §"Validation on hardware" and of the §"Risks" bullet will be measured against a smaller
  store and are not comparable across the change;
- §4's test 7, "eight flushes leave six states plus the root, and the two dropped ones are collected", must be
  rewritten to this note's rules: eight flushes of a short conversation at `W = 16 384` leave the end state
  alone (the idle flush on a new file offers the live state without the root, and every later flush under the
  window writes nothing), not six plus the root.
