// The disk tier of --serve's parked conversations (--conversation-disk-dir): the RAM cache's events
// (ConversationCache::set_on_change) drive it - an evicted image is written by a background thread to a block store,
// one `.bin` per layer-split stage, appended to, and one `.idx` naming its blocks - and read back when a request
// continues them, within a byte
// budget and (unless `persist`) this run only.  A read keeps the entry: the next eviction of the same conversation
// appends only its delta.  The budget evicts whole conversations, the least recently used first (`last_used_s`, which a
// read stamps; `written_s` for one never read); a conversation holding a pinned shared prefix is left while another can
// go.  `persist`: the files are bound to the session files' identity (model
// and settings), and the next start picks them up (the budget only).  A folder written by an older build (the v1
// session files one per stage) is removed at start: its entries are not read.
#pragma once

#include "strata/core/conversation_cache.hpp"
#include "strata/core/conversation_file.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <system_error>
#include <thread>
#include <vector>

namespace strata::core {

struct ConversationDiskOptions {
    std::string dir;                 ///< empty: the tier is off
    uint64_t budget_bytes = 0;       ///< at most this many bytes of files; 0: off
    uint64_t min_free_bytes = 0;     ///< a write needs the file plus this much free on the disk (session preflight)
    bool persist = false;            ///< keep the files for later runs, bound to `identity`
    SessionFileIdentity identity;    ///< persist: the session files' identity (model and settings fingerprints)
    /// --conversation-disk-flush-seconds: a park is written after this many quiet seconds (the debounce).  0 = off and
    /// today's behaviour byte for byte: nothing is queued at park time, the tier is written on eviction and at QUIT.
    int64_t flush_seconds = 0;
    /// --prompt-cache-every: a debounced flush writes an end state only when the end is this many tokens past the
    /// deepest stored state (or the entry holds no state at all).  0: a state at every flush.
    int64_t prompt_cache_every = 16384;
    /// Called with every diagnostic line (no trailing newline), from the caller's or the writer's thread.
    std::function<void(const std::string&)> log;
    /// Tests: called on the writer thread before each write starts.
    std::function<void()> before_write;
    /// Called on the writer thread for every image of a job before its K/V is read: STRATA_PARK_ASYNC's background
    /// copy may still be filling the park's host buffers, and the request thread cannot wait for it there without
    /// undoing the asynchronous park.  `generate.cpp` sets it to `conversation_park_join_for`; unset: nothing waits.
    std::function<void(const SavedConversation&)> join;
    /// Temporary, on by default, to be removed after phases 2 and 3 (docs/CONVERSATION_DISK_STORE_PHASE3.md, "the
    /// window of state blocks").  A read asks for the payload of every block below the state it lands on, so a
    /// conversation whose states are not bounded can ask for more RAM than the host has free, and its file is then
    /// never read (measured: a 4897 MiB entry refused at 5391 MiB free).  When a read is refused for RAM, the entry
    /// and its files go: a file that cannot be read is not a cache, and the bytes are free for the conversations that
    /// can.  Phases 2 and 3 bound the states per conversation (one state per flush, a window of the newest states), so
    /// the normal fix is there and this crutch goes.  This option is the way out: true keeps a refused entry
    /// (`--conversation-disk-keep-on-ram-refusal`); false, the default, removes it and its files.
    bool keep_on_ram_refusal = false;
};

/// What the tier did since it started (diagnostics).
struct ConversationDiskStats {
    size_t queued = 0, written = 0, write_failed = 0, ram_hits = 0, disk_hits = 0, read_failed = 0;
    size_t evicted = 0, waits = 0, fallbacks = 0;   ///< fallbacks: reads that landed on an earlier state (damage)
    size_t ram_drops = 0;   ///< entries removed from the disk when a read was refused for RAM (`keep_on_ram_refusal` off)
    uint64_t bytes_written = 0, bytes_read = 0;
    uint64_t tokens_back = 0;                 ///< prompt tokens given back (not read again) by ram or disk hits
    double write_ms = 0, read_ms = 0, wait_ms = 0;
};

// ---- container v2: a `.bin` per stage - a header, then immutable blocks - and one `.idx` naming every block.
// The index, not the file, says where the bytes are and what they hold: a `.bin` has no length and no trailer.
// Native-endian fields, counts before arrays.  The format is this phase's one new thing
// (docs/CONVERSATION_DISK_STORE_PHASE1.md, "Container v2"); the write path and the read path fill and consume it.

/// The `.bin`'s header (64 bytes).
constexpr size_t kConversationDiskBinHeader = 64;

/// What a `.bin` is bound to.  A file whose header names another model, setting, entry or stage is not read.
struct ConversationDiskBinHeader {
    uint64_t model = 0, config = 0;   ///< the session files' identity
    uint64_t id = 0;                  ///< the entry's id (it is in the file's name too)
    uint32_t stage = 0;               ///< which stage's image the blocks hold
};
/// The 64 header bytes: magic "STRACNV2", version 2, the header's size, then the fields above, zero reserved.
void conversation_disk_bin_header_write(uint8_t* out, const ConversationDiskBinHeader& header);
/// False when `bytes` is not a v2 header (too short, another magic or version, another header size).
bool conversation_disk_bin_header_read(const uint8_t* bytes, size_t n, ConversationDiskBinHeader& header);

/// One record of an index's block table: an immutable byte range of one stage's `.bin`.  `offset` and `size` locate
/// it; `hash_lo`/`hash_hi` are `session_hash64` of those bytes with two seeds; `prefix_digest` chains the block to
/// its place in the conversation (every token and image below `tokens_start`), so a block of another conversation,
/// or of another position in this one, never fits.  Phase 3 turns that digest into the store's key.
struct ConversationDiskBlock {
    static constexpr uint32_t kv = 1;      ///< the K/V of one page-aligned token range, for every layer of the stage
    static constexpr uint32_t state = 2;   ///< one checkpoint's state (gdn, ple, tails, dead, block_pos)
    uint32_t kind = 0;
    uint64_t offset = 0, size = 0;         ///< the block's bytes in the stage's `.bin`
    uint64_t hash_lo = 0, hash_hi = 0;
    int64_t tokens_start = 0;              ///< `kv`: the range's first token; `state`: its position
    int64_t tokens_end = 0;                ///< `kv`: the range's end; `state`: the same position
    uint64_t prefix_digest = 0;
    uint64_t used = 0;                     ///< `state` only: the LRU stamp, out of the payload
    bool pinned = false;                   ///< `state` only: the shared-prefix pin, out of the payload
};

/// One stage's `.bin` of an entry: the file, its layer carve and the blocks it holds, in write order (append order).
/// A layer split gives each stage its own range and layer count, so they are here and not on the entry.
struct ConversationDiskStage {
    std::string file;   ///< the `.bin`: the index holds its file name, a read resolves it against the tier's folder
    int64_t layer_lo = 0, layer_hi = 0;   ///< the stage image's layer carve
    uint32_t layers = 0;                  ///< its K/V layers (one per QSA layer of the carve, plus the draft)
    std::vector<ConversationDiskBlock> blocks;
};

/// One conversation as its index holds it.  `live` and `checkpoints` are keys only (token ids and images: what
/// `best()` matches); their bytes are blocks.
struct ConversationDiskEntry {
    uint64_t id = 0;                                ///< in the files' names
    std::vector<ConversationDiskStage> stages;      ///< the first stage's image, then one per later stage
    ConversationCheckpoint live;                    ///< keys only: the deepest state
    std::vector<ConversationCheckpoint> checkpoints;   ///< keys only, deepest first
    std::array<int64_t, 18> geometry{};             ///< v2: the image's geometry (the restore checks the runtime's)
    int64_t tokens = 0;                             ///< v2: the deepest `state` block's position: what `best()` claims
    bool cvec = true;
    uint64_t bytes = 0;                             ///< the entry's files
    std::string index;                              ///< the index file (set by a read)
    int64_t written_s = 0;                          ///< when the index was published (seconds since the epoch)
    int64_t last_used_s = 0;                        ///< v2: the LRU stamp, written with the next publish
    // Not in the index (the format is fixed): a read's own bookkeeping.
    int64_t verified = 0;                           ///< a read fell back on damage: the deepest state that verified
    size_t readers = 0;                             ///< a read is using its files: the writer removes it later
};

/// Writes the v2 index of `entry` to `path`: a temporary file beside it, then a rename, so a reader sees the whole
/// index or none.  Called only after the blocks it names are on disk.  `entry.written_s` is the caller's to stamp;
/// this does not change `entry`.
bool conversation_disk_index_write(const std::string& path, const ConversationDiskEntry& entry,
                                   const SessionFileIdentity& identity, std::string& error);
/// Reads a v2 index.  False when it is not one, its identity differs, or its shape is inconsistent (an unknown block
/// kind, an empty or overlapping block, `tokens` that is not the deepest state block's).  No `.bin` is touched: the
/// caller checks the table against its files.  `entry.index` becomes `path`.
bool conversation_disk_index_read(const std::string& path, const SessionFileIdentity& identity,
                                  ConversationDiskEntry& entry);

class ConversationDiskTier {
public:
    explicit ConversationDiskTier(ConversationDiskOptions options);
    ~ConversationDiskTier();
    ConversationDiskTier(const ConversationDiskTier&) = delete;
    ConversationDiskTier& operator=(const ConversationDiskTier&) = delete;

    bool enabled() const { return open_; }
    bool persistent() const { return open_ && options_.persist; }
    /// Creates the folder (0700 on POSIX) and starts the writer.  This tier's files a previous run left there are
    /// removed, or with `persist` taken in when their identity matches (the others removed).  False (and the tier
    /// stays off) when the folder cannot be used.
    bool open(std::string& error);

    /// The cache's events (ConversationCache::set_on_change).  With flush 0 nothing is written at park time, an
    /// eviction writes the whole image and a taken or superseded conversation's queued write is cancelled: phase 1's
    /// behaviour byte for byte.  With `--conversation-disk-flush-seconds N` a park arms a write due in `N` seconds
    /// (a second park of the same entry replaces it, keeping its id), and eviction or QUIT forces the write at once.
    void park(uint64_t serial, std::shared_ptr<const SavedConversation> image);
    /// The conversation `serial` left the cache (taken or superseded): a queued write of it is cancelled, and a write
    /// already running for it is waited for (the caller moves sub-objects out of the shared image next).
    void dropped(uint64_t serial);
    /// Writes `image` whole (eviction or QUIT), now: phase 1's put(), with the cache entry's serial.  Its RAM is freed
    /// when the write ends, stored or not.
    bool force(uint64_t serial, std::shared_ptr<const SavedConversation> image);
    /// Nothing the cache queued is restorable any more (#879's clear): the queue goes, deferred jobs too.
    void cancel_all();

    /// Queues `image` for the writer, as force() without a cache entry (tests, and a park the RAM cache refused).
    /// Every checkpoint is written with it (they hold no K/V), so a prompt that leaves the conversation early still
    /// resumes from one.  False when the tier is off or over budget.
    bool put(SavedConversation&& image);

    struct Match {
        uint64_t id = 0;        ///< the entry (stable while the writer runs; 0 = none)
        int64_t tokens = 0;
        bool live = false;
        uint64_t bytes = 0;     ///< the entry's files (queued: the image's estimate)
        bool queued = false;    ///< still in RAM: take() hands it back without reading a file
        /// The tokens a read of this match lands on: the deepest `state` block a read can rebuild (a read that fell
        /// back on damage lowers it).  The "read the file or read the tokens" estimate uses this one.
        int64_t restore_tokens = 0;
    };
    /// The longest prefix of `prompt` an entry holds (its live state or its checkpoint), queued or on the disk, as
    /// ConversationCache::best; ties prefer the newest entry.
    template<class Token>
    Match best(const std::vector<Token>& prompt, const std::vector<ConversationImageKey>& images, bool cvec) const {
        std::lock_guard<std::mutex> lock(m_);
        Match best;
        auto scan = [&](const Entry& e, bool queued) {
            if (e.cvec != cvec) return;
            auto consider = [&](const ConversationCheckpoint& c, bool live) {
                const int64_t n = conversation_prefix(c, prompt, images);
                const int64_t at = e.verified > 0 ? std::min(n, e.verified) : n;   // the deepest state a read got
                if (n > best.tokens || (n == best.tokens && n > 0 && e.id > best.id))
                    best = {e.id, n, live, e.bytes, queued, at};
            };
            consider(e.live, true);
            for (const auto& c : e.checkpoints) consider(c, false);
        };
        for (const auto& e : entries_) scan(e, false);
        for (const auto& job : queue_) scan(job.second.entry, true);
        if (writing_) scan(writing_entry_, false);   // its file appears when the write ends: take() waits for it
        return best;
    }

    /// Hands entry `id` back in `out`; the entry stays in the tier either way.  A queued image is the same allocation,
    /// moved out of the queue (a "RAM hit", no copy); a written one is read into a new image, its stage images
    /// included.  Only the blocks the deepest state a read can rebuild needs are read, and a damaged chain falls back
    /// to an earlier state.  `admit(need, why)` is asked before the blocks' payloads are allocated (session_file_read's
    /// RAM preflight).  `tokens` is the prefix the request matched (diagnostics).  On failure `out` is empty and
    /// `error` says why.  The entry stays: its files are a cache, except on a refusal for RAM, which removes it
    /// (temporary, `keep_on_ram_refusal`).
    bool take(uint64_t id, int64_t tokens, std::shared_ptr<SavedConversation>& out, std::string& error,
              const std::function<bool(uint64_t need_bytes, std::string& why)>& admit = {});

    /// Whether an image is being written or a queued write is due (their RAM is not back yet).  A job waiting for its
    /// due time is not RAM pressure: the bytes are the cache's (or an evicted image the writer already holds).
    bool pending() const;
    /// Makes every deferred job due now, then blocks until the queue is empty and no write runs; returns the ms it
    /// waited.  "Make room in RAM" and "shut down" never become timer waits.
    double wait_idle();
    /// #879: every conversation goes - queued, being written (waited for) and on the disk, files too.  Returns how many.
    size_t clear();

    size_t size() const;                    ///< entries on the disk
    uint64_t bytes() const;                 ///< their files' bytes
    ConversationDiskStats stats() const;
    /// One line of totals for the log.
    std::string summary() const;
    /// The file name prefix of this tier (tests).
    static const char* file_prefix() { return "strata-conv-"; }

private:
    using Entry = ConversationDiskEntry;   ///< the keys and the block table (container v2 above)
    struct Job {
        Entry entry;
        /// The image to write.  The cache shares this allocation (one allocation, two references) and hands it out
        /// as `const`; the tier keeps it mutable only so a read that matches a queued image can hand the allocation
        /// back to the restore path (take) without a copy - the writer itself only reads it.
        std::shared_ptr<SavedConversation> image;
        uint64_t serial = 0;                          ///< the cache entry the job came from (0: put(), no entry)
        std::chrono::steady_clock::time_point due;    ///< when the writer may start it
        bool forced = false;                          ///< eviction or QUIT: write the whole image, chain included
    };
    /// Queues `job` (keys, image, serial, due and shape filled): assigns its id, replaces an earlier job of the same
    /// serial (keeping that job's id), logs and wakes the writer.  False when the tier is off or over budget.
    bool enqueue(Job job);
    bool enqueue_image(uint64_t serial, std::shared_ptr<const SavedConversation> image, bool forced,
                       std::chrono::steady_clock::time_point due);
    void writer();
    void write(Job& job);                                    // without the lock
    void remove_locked(size_t index, bool delete_files);     // m_ held
    void log(const std::string& line) const;
    SessionFileIdentity file_identity() const;
    bool write_index_v2(Entry& e, std::string& error) const; // v2: the block store's index
    bool read_index(const std::string& path, Entry& e) const;   // v2 only: a v1 index is refused and removed
    bool check_files(const Entry& e) const;                  // start-up: a state block, a header and a length per stage
    std::string scan_folder(size_t& stale, std::error_code& ec);   // open(): take in or remove; its log part

    ConversationDiskOptions options_;
    bool open_ = false;
    uint64_t nonce_ = 0, next_id_ = 0, bytes_ = 0;
    mutable std::mutex m_;
    std::condition_variable cv_;
    std::deque<Entry> entries_;   ///< on the disk, least recently used first (the budget's order)
    /// The writer's work, ordered by due time (a multimap: two jobs may share a deadline).  Replace-by-serial keeps
    /// one job per cache entry, so this holds at most a handful.
    std::multimap<std::chrono::steady_clock::time_point, Job> queue_;
    bool writing_ = false;        ///< the writer holds a job (writing_entry_ is its keys)
    uint64_t writing_serial_ = 0; ///< ... and its cache entry (0: put()), for a take() that must wait for it
    Entry writing_entry_;
    bool stop_ = false;
    std::thread thread_;
    ConversationDiskStats stats_;
    // a queued image published into an existing entry keeps that entry's id, so the id `put` gave the image stops
    // existing.  The map (from the queued id, to the entry) lets a read that matched the queued image find the entry.
    std::vector<std::pair<uint64_t, uint64_t>> published_;
};

}  // namespace strata::core
