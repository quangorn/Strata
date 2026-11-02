// The disk tier of --serve's parked conversations (--conversation-disk-dir): the RAM cache's evictions
// (ConversationCache::set_on_evict) are written to session files (conversation_file.hpp, one per layer-split stage)
// by a background thread and read back when a request continues them, within a time limit and a byte budget.
// By default only this process reads its files back, and the folder's files of this tier are removed at start and at
// the end.  `persist`: the files are bound to the session files' identity (model and settings) instead, a small index
// file beside each conversation lists its tokens, and the next start picks them up (no time limit, the budget only).
#pragma once

#include "strata/core/conversation_cache.hpp"
#include "strata/core/conversation_file.hpp"

#include <array>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
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
    /// Called with every diagnostic line (no trailing newline), from the caller's or the writer's thread.
    std::function<void(const std::string&)> log;
    /// Tests: called on the writer thread before each write starts.
    std::function<void()> before_write;
};

/// What the tier did since it started (diagnostics).
struct ConversationDiskStats {
    size_t queued = 0, written = 0, write_failed = 0, ram_hits = 0, disk_hits = 0, read_failed = 0;
    size_t evicted = 0, waits = 0;
    uint64_t bytes_written = 0, bytes_read = 0;
    uint64_t tokens_back = 0;                 ///< prompt tokens given back (not read again) by ram or disk hits
    double write_ms = 0, read_ms = 0, wait_ms = 0;
};

// ---- container v2: a `.bin` per stage - a header, then immutable blocks - and one `.idx` naming every block.
// The index, not the file, says where the bytes are and what they hold: a `.bin` has no length and no trailer.
// Native-endian fields, counts before arrays, as the v1 index.  The format is this phase's one new thing
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
    std::vector<std::string> files;                 ///< v1 only: one session file per stage (the v1 read path)
    ConversationCheckpoint live;                    ///< keys only: the deepest state
    std::vector<ConversationCheckpoint> checkpoints;   ///< keys only, deepest first
    std::array<int64_t, 18> geometry{};             ///< v2: the image's geometry (the restore checks the runtime's)
    int64_t tokens = 0;                             ///< v2: the deepest `state` block's position: what `best()` claims
    bool cvec = true;
    uint64_t bytes = 0;                             ///< the entry's files
    std::string index;                              ///< the index file (set by a read)
    int64_t written_s = 0;                          ///< when the index was published (seconds since the epoch)
    int64_t last_used_s = 0;                        ///< v2: the LRU stamp, written with the next publish
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

    /// Queues `image` for the writer; its RAM is freed when its write ends, stored or not.  Every checkpoint is written
    /// with it (they hold no K/V), so a prompt that leaves the conversation early still resumes from one.  False when
    /// the tier is off or over budget.
    bool put(SavedConversation&& image);

    struct Match {
        uint64_t id = 0;        ///< the entry (stable while the writer runs; 0 = none)
        int64_t tokens = 0;
        bool live = false;
        uint64_t bytes = 0;     ///< the entry's files (queued: the image's estimate)
        bool queued = false;    ///< still in RAM: take() hands it back without reading a file
        /// The tokens a read of this match lands on: a v1 file holds exactly the matched state, while a v2 entry
        /// reports the position of the `state` block its table can rebuild (at most one checkpoint interval behind
        /// `tokens`).  The "read the file or read the tokens" estimate uses this one.
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
                if (n > best.tokens || (n == best.tokens && n > 0 && e.id > best.id)) best = {e.id, n, live, e.bytes, queued};
            };
            consider(e.live, true);
            for (const auto& c : e.checkpoints) consider(c, false);
        };
        for (const auto& e : entries_) scan(e, false);
        for (const auto& j : queue_) scan(j.entry, true);
        if (writing_) scan(writing_entry_, false);   // its file appears when the write ends: take() waits for it
        best.restore_tokens = best.tokens;   // a v1 file holds the matched state whole; the v2 read path lowers this
        return best;
    }

    /// Hands entry `id` back in `out` (its stage images included) and removes it from the tier either way: from RAM
    /// when it is still queued, else from the disk (waiting for its write when it is being written).  `admit(need,
    /// why)` is asked before each file's payload is allocated (session_file_read's RAM preflight).  `tokens` is what
    /// the request reuses (diagnostics).  On failure `out` is empty and `error` says why.
    bool take(uint64_t id, int64_t tokens, SavedConversation& out, std::string& error,
              const std::function<bool(uint64_t need_bytes, std::string& why)>& admit = {});

    /// Whether images wait in the queue or are being written (their RAM is not back yet).
    bool pending() const;
    /// Blocks until the queue is empty and no write runs; returns the ms it waited.
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
    using Entry = ConversationDiskEntry;   ///< the v1 keys and files, or the v2 block table (container v2 above)
    struct Job {
        Entry entry;
        SavedConversation image;
    };
    void writer();
    void write(Job& job);                                    // without the lock
    void remove_locked(size_t index, bool delete_files);     // m_ held
    void log(const std::string& line) const;
    SessionFileIdentity file_identity() const;
    bool write_index_v2(Entry& e, std::string& error) const; // v2: the block store's index
    bool read_index(const std::string& path, Entry& e) const;   // v1 and v2
    std::string scan_folder(size_t& stale, std::error_code& ec);   // open(): take in or remove; its log part

    ConversationDiskOptions options_;
    bool open_ = false;
    uint64_t nonce_ = 0, next_id_ = 0, bytes_ = 0;
    mutable std::mutex m_;
    std::condition_variable cv_;
    std::deque<Entry> entries_;   ///< on the disk, oldest first
    std::deque<Job> queue_;       ///< waiting for the writer, oldest first
    bool writing_ = false;        ///< the writer holds a job (writing_entry_ is its keys)
    Entry writing_entry_;
    bool stop_ = false;
    std::thread thread_;
    ConversationDiskStats stats_;
};

}  // namespace strata::core
