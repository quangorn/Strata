# [Engine] the disk tier: the measurements behind the store plan

This file is the evidence base for
[phase 1](CONVERSATION_DISK_STORE_PHASE1.md), [phase 2](CONVERSATION_DISK_STORE_PHASE2.md) and
[phase 3](CONVERSATION_DISK_STORE_PHASE3.md). It is a record of measured runs, not a proposal: the
earlier proposal that lived here has been split into the three phase documents, and "what the earlier
proposal said and where it went" at the end says which piece went where.

It is kept apart from the phase documents because they are design notes with nothing run on hardware,
while this is the opposite: numbers only, each with what it was measured on.

## The drive used from now on

A new SSD was installed on 1 November 2026 and the store was moved to it (`E:\strata-conv-data`). The
old HDD is no longer used for conversations.

Measured on RTX 4080 16 GB, 48 GB of host RAM, Windows 10, engine 0.1.55, a local source build,
Swift-Qwen3.8-Flash-Next-GS-RCO-IQ2_XS, profile `iq2_xs-low_ram`:

```
--conversation-cache-mib 2048 --conversation-cache-slots 8 --conversation-cache-min-tokens 16384
--conversation-cache-min-free-mib 512 --conversation-disk-dir E:\strata-conv-data
--conversation-disk-mib 153600 --conversation-disk-persist on
```

The evening of 1 November, after the move, 4 engine runs, from
`C:\Users\karam\AppData\Local\Strata\launch\iq2_xs-low_ram.engine.log` (from the first start on `E:`):

| | |
|---|---|
| disk writes | 47 conversations, 6 097 745 tokens, 88 306 MiB, 91.7 s; mean 1 009 MB/s, 66 468 tokens/s |
| disk reads | 42, 3 861 768 tokens, 77 679 MiB, 81.2 s; mean 1 003 MB/s, 47 573 tokens/s |
| the last run's own totals | 44 written (83 749 MiB, 1 022 MB/s), 39 disk hits (73 259 MiB, 1 014 MB/s) |
| waits for a queued write | 4, at 1 409, 1 763, 2 520 and 2 667 ms (`their RAM was needed`) |
| RAM hits, RAM evictions, dropped writes | 0, 0, 0 |
| `not read` | 4, all the same shape: `its 7362 more tokens read in ~2.9-3.5 s, its file (2 048-2 395 MiB) in ~3.5-3.8 s` |
| QUIT | `saved for the next start in 3.9 s` |
| entries left on the disk | 13, 21 118 MiB |
| tokens across those entries | 1 299 224 |
| density | 15.2 KB/token over the 88 306 MiB; per write from 13.1 to 40.0 KB/token |
| prefix check (below) | 14 429 MiB of 21 118 MiB duplicated — **68.3%**; no entry is fully inside another |

### The prefix check, and what it found

Reading the 13 `.idx` files of `E:\strata-conv-data` (the index lists every checkpoint's token ids,
`conversation_disk.cpp:164-229`): for each entry, the longest token prefix it shares with any other
entry, times its own bytes per token. That is the bytes a store with one copy per prefix would not
write.

| | |
|---|---|
| duplicated | 14 429 MiB of 21 118 MiB (68.3%) |
| no entry fully inside another | 0 MiB (as on the old drive) |
| shared system prompts | 9 372 tokens, and 7 362-7 366, and 7 196 |
| the large case | one conversation written again as it grew: 177 006, 178 725 and 196 984 tokens shared between pairs of entries, ~2.3-2.5 GiB each time |

The large case is the new finding of this measurement and it is not a shared system prompt: it is the
**same conversation written whole again** at a longer length, in a new entry, because nothing appends
(`conversation_disk.cpp:367` writes a fresh image every time) and nothing removes the shorter copy
that no read ever picks (`best()` always takes the longest prefix). On the old drive with a 256 GiB
budget the same effect existed but the day only produced 6 entries and 900 MiB; with a 150 GiB budget
and 13 entries it is the majority of the store.

### The read that pays for a whole file to get a few tokens

Three of the four `not read` skips, and the disk hits around them, show the second cost: a restore of
7 362 more tokens read (or refused to read) a **2 395 MiB** file, because a file is the unit
(`conversation_file.cpp:1211`: the payload length is checked against the file size, the whole payload
is hashed at `:1232`). 7 362 tokens of K/V is about 70 MiB and a state block is 118 MiB, so a
block-addressed read would be ~190 MiB instead of 2 395 MiB — 8% of the bytes for the same tokens.

## The HDD it replaced (kept for contrast)

Same PC before the move, `D:\tmp\strata-conv-data`, `--conversation-cache-mib 6144
--conversation-disk-mib 262144 --conversation-disk-persist on`, one day of OpenCode sessions, 3 runs:

| | |
|---|---|
| disk writes | 11 conversations, 432 471 tokens, 11 839 MiB, 80.7 s; mean 159.7 MB/s, 5 362 tokens/s |
| disk reads | 5, 168 325 tokens, 5 083 MiB, 57.4 s; mean 92.6 MB/s, 2 935 tokens/s |
| waits for a queued write | 2, 12.1 s and 17.3 s |
| entries left on the disk | 6, 6 755 MiB |
| density | 19.9 KB/token for a 76 200-token copy, 44.2 KB/token for a 20 121-token copy (5 checkpoints) |
| prefix check | ~900 MiB of 6 755 MiB duplicated (13.3%); no entry fully inside another |
| `not read` | 1: `its 32819 more tokens read in ~12.8 s, its file (1168 MiB) in ~13.7 s` |

The old note's rule of thumb — "the fixed part is the live state plus 4-6 checkpoints, ~118 MiB each"
— comes from here and still holds: the state part does not depend on the drive.

## What each number decides

| number | what it decides |
|---|---|
| 47 writes of ~1 878 MiB mean on the new drive | the whole image is rewritten per eviction and per park — phase 1 appends instead |
| 14 429 MiB of 21 118 duplicated, 68% | the same conversation, and the same system prompt, stored once per copy — phase 1 (append, drop the superseded entry) and phase 3 (one copy of a shared prefix) |
| a 2 395 MiB file read for 7 362 tokens | a file is the unit; blocks make this ~190 MiB — phase 1. Measured on this PC 2026-11-03/04: a read takes exactly the blocks below the state it lands on (34 reads matched to their entries, ratio read/needed 1.00), which is **87-100% of the entry** — the saving is only the states above the landing state, and a read that lands on the entry's deepest state takes all of it. The shallow case (~190 MiB for 7362 tokens) has not been seen: the shallow reads are the ones the `not read` rule refused, and those entries are gone from the folder |
| 4 waits at 1.4-2.7 s, QUIT in 3.9 s | the trigger still sits at eviction and QUIT; on a fast drive it is affordable but still a burst — phase 2 |
| 15.2 KB/token, ~118 MiB per state | the K/V/state split, and the "one state or the whole chain per flush" question phase 2 leaves to this number |
| `pinned` is not in the index, `used` is inside the payload | phase 1 moves both out of the payload |
| 4 `not read` of 42 | with block reads the heuristic would rarely fire; it exists because a restore costs a whole file |

## What the earlier proposal said and where it went

The first version of this document proposed a change inside `ConversationDiskTier::put()`
(`src/core/conversation_disk.cpp:306`): drop the entries a new image supersedes, and skip a park whose
image the disk already holds. Each piece now has a home:

| the earlier proposal | where it went |
|---|---|
| drop disk entries superseded by a new image (`superseded_at`, `conversation_cache.hpp:522`) | **phase 1**: the flush appends to the entry whose chain is a prefix of it and removes the entries it supersedes. The 68% above is why this is now a rule, not an option |
| "not queued (the disk holds a longer copy)" | **phase 2**: a debounced flush computes its delta, and a delta of zero blocks writes nothing |
| share one prefix image between branches (explicitly not proposed there) | **phase 3**, with the identity and position guards the earlier note did not have |
| the pinned guard has no disk side | **phase 1** stores `pinned` in the index; **phase 3** keeps the root and a window of states |
| the byte budget removes "oldest written first" | **phase 1**: the budget evicts whole conversations by `last_used` |
| `--conversation-disk-persist exit-only` writes nothing until QUIT | still true and still supported; phase 2's flag defaults to `0`, which is the same behaviour |

## Still open

- The duplication, measured on the store the block serves now (this PC, four snapshots of the live
  folder): **at 20:31 on 2026-11-03** — 58 entries, 76 397 MiB, K/V 36 314 MiB, 4364 MiB duplicated =
  12.0% of the K/V, 5.7% of the store, the live shared prefixes 7493-7664 tokens (the system prompt,
  12 entries); **at 00:53 on 2026-11-04** — 37 entries, 74 939 MiB, K/V 43 863 MiB, **18 892 MiB
  duplicated = 43.1% of the K/V, 25.2% of the store**, the biggest pairs sharing 116 022-197 120 tokens of
  one dialogue; **at 07:29 on 2026-11-04** — 35 entries, 76 155 MiB, K/V 46 543 MiB, **16 914 MiB
  duplicated = 36.3% of the K/V, 22.2% of the store**, the biggest pair sharing 197 120 tokens of the same
  dialogue; **at 02:23 on 2026-11-06**, after a full day with `--conversation-disk-flush-seconds 60` on the
  serving profile (97 writes, 54 appended) — 38 entries, 76 539 MiB, K/V 46 917 MiB, states 29 500 MiB,
  **13 706 MiB duplicated = 29.2% of the K/V, 17.9% of the store**, the biggest pair sharing 127 726 tokens
  (1792.5 MiB), the shared system prompt band 6731-7798 tokens, 0 entries fully inside another — and after
  three more debounced days, on 2026-11-08: 64 entries, 76 624 MiB, **58 513 MiB duplicated = 76.4% of the
  store** (117.7% of the K/V — the tool prices each entry's bytes per token, K/V and states together,
  for the longest prefix it shares with any other entry, each entry counted once:
  `tools/conversation_store_scan.py:133-146`), 1 entry fully inside another, the biggest pair sharing
  201 113 tokens. The honest number beside that price, read by payload hash on the same folder
  (2026-11-08): **26 034 MiB of K/V are extra copies** (1524 extra copies of 1314 shared blocks) =
  34.5% of the store; K/V inside shared prefixes 38 232 MiB = 50.7%; the first copy that has to exist
  is 12 198 MiB; 0 duplicate bytes outside a shared prefix; 3 entries of 65 have ever been read back.
  The share moves as the run goes: 75.8% two hours after this scan, 72.7% the same afternoon.
  The number moves with what the budget keeps (103 removals
  in the live run), and the case the baseline measured (the same conversation written whole again as it grew)
  is what the live traffic still produces: a continuation whose image keys changed goes to a new entry with
  its whole prefix, and `0 superseded` in every one of the 403 writes, and in the 429 writes of 2026-11-05
  to 2026-11-08, means the disk-side supersede rule has nothing to remove: the RAM cache's tenth rule (#342)
  drops the copies it can match before they reach the disk, and the disk rule asks for an exact prefix, so
  the divergent copies are invisible to it ("Why supersede is 0 on live traffic" in
  [CONVERSATION_DISK_STORE_PHASE3.md](CONVERSATION_DISK_STORE_PHASE3.md)). The delta appends merge growing
  conversations, and on this PC's traffic the duplicated prefix grew to the dominant part of the folder. The
  rest is phase 3's block sharing.
  The prefix check ran on ad-hoc scripts until 2026-11-06; the reader is now a tool in the repo,
  `tools/conversation_store_scan.py`, so phase 3's "same day re-run with the same tool" has one.
- The pin still has no *semantic* disk side: phase 1 stores the flag, but nothing on the disk branches
  from a prefix the way a suffix query does in RAM. The folder has **0 pinned state blocks** in the live
  traffic, so the disk pin is tested offline only.
- `exit-only`'s measured cost on the old drive (40 writes, 197 GiB in a day, both disk hits from the
  QUIT save, `DETAILS.md:852-854`) is the argument for phase 2, not against `exit-only`.
- Phase 1 is measured in part on this PC (the numbers above, and "Live traffic on the serving profile" in
  [CONVERSATION_DISK_STORE_PHASE1_STEPS.md](CONVERSATION_DISK_STORE_PHASE1_STEPS.md)). Phase 2 is measured
  in part on this PC's live traffic ("Live days on the serving profiles" in
  [CONVERSATION_DISK_STORE_PHASE2_STEPS.md](CONVERSATION_DISK_STORE_PHASE2_STEPS.md), 2026-11-05 to
  2026-11-08). Phase 3 is not measured. Each phase document has its own validation plan, and the numbers
  above are its baseline.
