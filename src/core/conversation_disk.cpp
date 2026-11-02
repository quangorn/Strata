// src/core/conversation_disk.cpp - the disk tier of the parked conversations.  See the header.
#include "strata/core/conversation_disk.hpp"

#include "strata/core/conversation_file.hpp"

#include <algorithm>
#include <charconv>
#include <chrono>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <random>
#include <set>
#include <system_error>
#include <utility>
#if defined(__linux__)
#include <sys/resource.h>
#include <sys/syscall.h>
#include <unistd.h>
#endif

namespace strata::core {
namespace {

namespace fs = std::filesystem;

// The second half of every file's identity: with this run's random number as the first, a file another run (or
// another tier) wrote is refused on read.
constexpr uint64_t kTierConfig = 0x5354524154414449ull;   // "STRATADI"

// ids and images only: the part of a checkpoint best() matches against
ConversationCheckpoint key_of(const ConversationCheckpoint& c) {
    ConversationCheckpoint k;
    k.ids = c.ids;
    k.imgs = c.imgs;
    return k;
}

// a file of this tier (also a temporary one session_file_write left behind: ".<name>.<hex>.tmp")
bool tier_file(const std::string& name) {
    const std::string prefix = ConversationDiskTier::file_prefix();
    return name.rfind(prefix, 0) == 0 || name.rfind("." + prefix, 0) == 0;
}

void remove_file(const std::string& path) {
    std::error_code ec;
    fs::remove(fs::path(path), ec);
}

// The files an entry on the disk owns: its `.bin` per stage (and, for the caller, its index).
std::vector<std::string> files_of(const ConversationDiskEntry& e) {
    std::vector<std::string> out;
    for (const auto& stage : e.stages) out.push_back(stage.file);
    return out;
}

// The budget's order (phase 1, step 5): when the conversation was last used.  A read stamps `last_used_s`, a publish
// stamps `written_s`, so the later of the two is the use - an entry written after a read is not old.  Seconds, as the
// index holds them; a tie falls to the older id, the order entries are created in.
int64_t lru_s(const ConversationDiskEntry& e) { return std::max(e.last_used_s, e.written_s); }

// The whole conversation the budget removes next: the least recently used one that is neither the entry a write is
// extending nor one a read holds.  `entries.size()` when nothing may go (the write then goes over the budget, which
// the RAM cache does too: an entry is never split and never left half-written).
size_t lru_victim(const std::deque<ConversationDiskEntry>& entries, uint64_t except) {
    size_t victim = entries.size();
    for (size_t i = 0; i < entries.size(); ++i) {
        const auto& e = entries[i];
        if (e.id == except || e.readers > 0) continue;
        if (victim == entries.size() || lru_s(e) < lru_s(entries[victim]) ||
            (lru_s(e) == lru_s(entries[victim]) && e.id < entries[victim].id))
            victim = i;
    }
    return victim;
}

// The id and stage a `.bin`'s name holds ("strata-conv-<nonce>-<id>-<stage>.bin").  False when the name is not that
// shape (a file a test or a person renamed): then only the header's stage can be checked against the index.
bool name_ids(const std::string& file, uint64_t& id, uint32_t& stage) {
    const std::string name = fs::path(file).filename().string();
    const std::string prefix = ConversationDiskTier::file_prefix();
    if (name.rfind(prefix, 0) != 0) return false;
    std::string rest = name.substr(prefix.size());
    if (rest.size() < 4 || rest.compare(rest.size() - 4, 4, ".bin") != 0) return false;
    rest.resize(rest.size() - 4);
    auto number = [](const std::string& s, unsigned& v) {
        const auto r = std::from_chars(s.data(), s.data() + s.size(), v);
        return !s.empty() && r.ec == std::errc{} && r.ptr == s.data() + s.size();
    };
    const size_t last = rest.rfind('-');
    if (last == std::string::npos) return false;
    unsigned nstage = 0;
    if (!number(rest.substr(last + 1), nstage)) return false;
    rest.resize(last);
    const size_t second = rest.rfind('-');
    if (second == std::string::npos) return false;
    unsigned long long nid = 0;
    const std::string id_s = rest.substr(second + 1);
    const auto r = std::from_chars(id_s.data(), id_s.data() + id_s.size(), nid);
    if (id_s.empty() || r.ec != std::errc{} || r.ptr != id_s.data() + id_s.size()) return false;
    id = nid;
    stage = nstage;
    return true;
}

// The index file of a persistent entry: native-endian fields (the session files refuse a big-endian build too),
// counts before arrays.  Magic "STRCIDX2"; a v1 index ("STRCIDX1") no longer reads and is removed at start.
constexpr uint64_t kIndexMagicV2 = 0x3258444943525453ull;   // "STRCIDX2"

// The v2 container's `.bin`: magic "STRACNV2", version 2, a 64-byte header (conversation_disk.hpp).
constexpr uint64_t kBinMagic = 0x32564E4341525453ull;   // "STRACNV2"
constexpr uint32_t kBinVersion = 2;

struct IndexWriter {
    std::string b;
    template<class T> void put(T v) { b.append(reinterpret_cast<const char*>(&v), sizeof v); }
    void str(const std::string& s) { put<uint32_t>((uint32_t) s.size()); b += s; }
    void key(const ConversationCheckpoint& c) {
        put<uint32_t>((uint32_t) c.ids.size());
        b.append(reinterpret_cast<const char*>(c.ids.data()), c.ids.size() * sizeof(int32_t));
        put<uint32_t>((uint32_t) c.imgs.size());
        for (const auto& i : c.imgs) { put<int64_t>(i.start); put<uint64_t>(i.hash); }
    }
};

struct IndexReader {
    const std::string& b;
    size_t at = 0;
    bool ok = true;
    size_t left() const { return b.size() - at; }
    template<class T> T get() {
        T v{};
        if (left() < sizeof v) { ok = false; return v; }
        std::memcpy(&v, b.data() + at, sizeof v);
        at += sizeof v;
        return v;
    }
    std::string str() {
        const uint32_t n = get<uint32_t>();
        if (!ok || left() < n) { ok = false; return {}; }
        at += n;
        return b.substr(at - n, n);
    }
    bool key(ConversationCheckpoint& c) {
        const uint32_t n = get<uint32_t>();
        if (!ok || left() / sizeof(int32_t) < n) return ok = false;
        c.ids.resize(n);
        std::memcpy(c.ids.data(), b.data() + at, n * sizeof(int32_t));
        at += n * sizeof(int32_t);
        const uint32_t m = get<uint32_t>();
        if (!ok || left() / 16 < m) return ok = false;
        for (uint32_t i = 0; i < m; ++i) c.imgs.push_back({get<int64_t>(), get<uint64_t>()});
        return ok;
    }
};

// The v2 index's bytes: identity, the LRU stamps, the keys, then one block table per stage (entry level, then a
// count and the records: offset, size, hash_lo, hash_hi, kind, tokens_start, tokens_end, prefix_digest, used, pinned).
void put_block(IndexWriter& out, const ConversationDiskBlock& b) {
    out.put<uint64_t>(b.offset);
    out.put<uint64_t>(b.size);
    out.put<uint64_t>(b.hash_lo);
    out.put<uint64_t>(b.hash_hi);
    out.put<uint32_t>(b.kind);
    out.put<int64_t>(b.tokens_start);
    out.put<int64_t>(b.tokens_end);
    out.put<uint64_t>(b.prefix_digest);
    out.put<uint64_t>(b.used);
    out.put<uint8_t>(b.pinned ? 1 : 0);
}

void index_v2_bytes(IndexWriter& out, const ConversationDiskEntry& e, const SessionFileIdentity& id) {
    out.put(kIndexMagicV2);
    out.put(id.model);
    out.put(id.config);
    out.put<int64_t>(e.written_s);
    out.put<int64_t>(e.last_used_s);
    out.put<uint8_t>(e.cvec ? 1 : 0);
    out.put<uint64_t>(e.bytes);
    for (int64_t g : e.geometry) out.put<int64_t>(g);
    out.put<int64_t>(e.tokens);
    out.key(e.live);
    out.put<uint32_t>((uint32_t) e.checkpoints.size());
    for (const auto& c : e.checkpoints) out.key(c);
    out.put<uint32_t>((uint32_t) e.stages.size());
    for (const auto& s : e.stages) {
        out.str(fs::path(s.file).filename().string());   // the name: a read resolves it against the folder
        out.put<int64_t>(s.layer_lo);
        out.put<int64_t>(s.layer_hi);
        out.put<uint32_t>(s.layers);
        out.put<uint32_t>((uint32_t) s.blocks.size());
        for (const auto& b : s.blocks) put_block(out, b);
    }
}

// Every check the shape allows without the `.bin`s: an unknown kind, an empty or overlapping block, a position that
// is not the block's token range's.  Offsets start at the end of the 64-byte header and only grow.
bool read_index_v2(const std::string& b, const SessionFileIdentity& id, ConversationDiskEntry& e) {
    IndexReader in{b};
    if (in.get<uint64_t>() != kIndexMagicV2 || in.get<uint64_t>() != id.model || in.get<uint64_t>() != id.config)
        return false;
    e.written_s = in.get<int64_t>();
    e.last_used_s = in.get<int64_t>();
    e.cvec = in.get<uint8_t>() != 0;
    e.bytes = in.get<uint64_t>();
    for (auto& g : e.geometry) g = in.get<int64_t>();
    e.tokens = in.get<int64_t>();
    if (!in.key(e.live)) return false;
    const uint32_t nck = in.get<uint32_t>();
    if (!in.ok || nck > in.left() / 8) return false;   // a key is at least its two counts
    for (uint32_t i = 0; in.ok && i < nck; ++i) {
        ConversationCheckpoint c;
        if (in.key(c)) e.checkpoints.push_back(std::move(c));
    }
    const uint32_t nstages = in.get<uint32_t>();
    if (!in.ok || nstages == 0 || nstages > in.left() / 20) return false;
    for (uint32_t s = 0; in.ok && s < nstages; ++s) {
        ConversationDiskStage stage;
        stage.file = in.str();
        stage.layer_lo = in.get<int64_t>();
        stage.layer_hi = in.get<int64_t>();
        stage.layers = in.get<uint32_t>();
        const uint32_t nblocks = in.get<uint32_t>();
        if (!in.ok || stage.file.empty() || stage.layers > 1u << 16 || nblocks > in.left() / 64) return false;
        if (stage.layer_lo < 0 || stage.layer_hi < stage.layer_lo) return false;
        uint64_t end = kConversationDiskBinHeader;   // the first block starts after the header, and they only grow
        for (uint32_t k = 0; in.ok && k < nblocks; ++k) {
            ConversationDiskBlock block;
            block.offset = in.get<uint64_t>();
            block.size = in.get<uint64_t>();
            block.hash_lo = in.get<uint64_t>();
            block.hash_hi = in.get<uint64_t>();
            block.kind = in.get<uint32_t>();
            block.tokens_start = in.get<int64_t>();
            block.tokens_end = in.get<int64_t>();
            block.prefix_digest = in.get<uint64_t>();
            block.used = in.get<uint64_t>();
            const uint8_t pinned = in.get<uint8_t>();
            if (!in.ok || (block.kind != ConversationDiskBlock::kv && block.kind != ConversationDiskBlock::state))
                return false;
            if (block.size == 0 || block.offset < end || block.offset > UINT64_MAX - block.size) return false;
            const bool range = block.kind == ConversationDiskBlock::kv ? block.tokens_end > block.tokens_start
                                                                       : block.tokens_end == block.tokens_start;
            if (!range || pinned > 1) return false;
            block.pinned = pinned != 0;
            end = block.offset + block.size;
            stage.blocks.push_back(std::move(block));
        }
        e.stages.push_back(std::move(stage));
    }
    if (!in.ok || in.left() != 0) return false;
    // `tokens` is what best() may claim: the deepest state block's position
    bool have_state = false;
    int64_t deepest = 0;
    for (const auto& stage : e.stages)
        for (const auto& block : stage.blocks)
            if (block.kind == ConversationDiskBlock::state && (!have_state || block.tokens_end > deepest)) {
                deepest = block.tokens_end;
                have_state = true;
            }
    return !have_state || e.tokens == deepest;
}

// ---- the block store's write path (phase 1, step 3) and the read path take() uses (step 4: only the blocks the
// state a read lands on needs, the chained digest checked, and a damaged chain falls back to an earlier state).

constexpr int64_t kBlockTokens = 2048;   // a kv block holds at most this many tokens, rounded down to the page

// A block's 128 bits: one SessionHasher run per seed.  The hashes detect accidental corruption, not tampering (as
// the session files').
constexpr uint64_t kBlockSeedLo = 0x535452414b56324cull;   // "STRAKV2L"
constexpr uint64_t kBlockSeedHi = 0x535452414b563248ull;   // "STRAKV2H"

int64_t round_up(int64_t n, int64_t page) { return ((n + page - 1) / page) * page; }

// The layout a stage image's K/V layers share: its extent, its page, and the bytes one cell (and one indexer row)
// takes in each part.  All of it is derived from the image and checked, so an image whose parts are not whole cells,
// or whose indexer rows are not one per page, is refused instead of being sliced wrongly.
struct KvPlan {
    int64_t upto = 0, page = 0, limit = 0;
    std::vector<std::array<size_t, 4>> stride;   // per layer: k, v, k_scale, v_scale bytes per cell
    std::vector<size_t> row;                     // per layer: one indexer row's bytes (0: no indexer)
};

bool kv_plan(const SavedConversation& img, KvPlan& plan, std::string& error) {
    plan = {};
    plan.upto = (int64_t) img.live.ids.size();
    if (img.kv.empty()) return true;
    plan.page = img.kv.front().page_size;
    if (plan.page <= 0) {
        error = "the image's K/V has no page size";
        return false;
    }
    plan.limit = (plan.upto / plan.page) * plan.page;
    const int64_t cells = plan.upto ? round_up(plan.upto, plan.page) : 0;
    for (const auto& kv : img.kv) {
        if (kv.page_size != plan.page || kv.cells != cells || kv.heads <= 0 || kv.head_dim <= 0) {
            error = "the image's K/V layers disagree on their geometry";
            return false;
        }
        std::array<size_t, 4> stride{};
        const ConversationBuffer* parts[4] = {&kv.k, &kv.v, &kv.k_scale, &kv.v_scale};
        for (size_t p = 0; p < 4; ++p) {
            if (!cells) {
                if (parts[p]->size()) {
                    error = "a K/V part of an empty image is not empty";
                    return false;
                }
                continue;
            }
            if (parts[p]->size() % (size_t) cells) {
                error = "a K/V part is not a whole number of cells";
                return false;
            }
            stride[p] = parts[p]->size() / (size_t) cells;
        }
        size_t row = 0;
        if (kv.pooled_rows > 0) {
            if (kv.idx_dim <= 0 || kv.pooled.size() % (size_t) kv.pooled_rows) {
                error = "the indexer's rows are not a whole number of bytes";
                return false;
            }
            row = kv.pooled.size() / (size_t) kv.pooled_rows;
            // the store slices the indexer's rows on the page boundary, which is the indexer's block in this engine
            if (row != (size_t) kv.idx_dim * sizeof(float) || kv.pooled_rows != plan.upto / plan.page + 1) {
                error = "the indexer's rows are not one per page of tokens";
                return false;
            }
        }
        plan.stride.push_back(stride);
        plan.row.push_back(row);
    }
    return true;
}

// The chained prefix digest: every token and image below `start`, seeded by the digest of the block before this one
// in the stage's table.  It binds a block to its place in the conversation, so a block of another conversation (or of
// another position in this one) never fits; phase 3 turns it into the store's key.
uint64_t prefix_digest(const std::vector<int32_t>& ids, const std::vector<ConversationImageKey>& imgs, size_t start,
                       uint64_t previous) {
    SessionHasher h(previous);
    if (start) h.update(ids.data(), start * sizeof(int32_t));
    for (const auto& image : imgs) {
        if (image.start >= (int64_t) start) break;
        h.update(&image, sizeof image);
    }
    return h.digest();
}

// One stage's `.bin`, appended to: the 64-byte header, then blocks.  A write starts at the end of the last indexed
// block, so a torn tail (a write that ended before its index was published) is overwritten by the next append.
class BinFile {
public:
    bool open_new(const std::string& path, const ConversationDiskBinHeader& header, std::string& error) {
        path_ = path;
        f_.open(fs::path(path), std::ios::binary | std::ios::out | std::ios::trunc);
        if (!f_) {
            error = "cannot create " + path;
            return false;
        }
        uint8_t bytes[kConversationDiskBinHeader];
        conversation_disk_bin_header_write(bytes, header);
        f_.write(reinterpret_cast<const char*>(bytes), (std::streamsize) kConversationDiskBinHeader);
        if (!f_) {
            error = "cannot write the header of " + path;
            return false;
        }
        at_ = kConversationDiskBinHeader;
        return true;
    }
    bool open_append(const std::string& path, const ConversationDiskBinHeader& header, uint64_t end,
                     std::string& error) {
        path_ = path;
        f_.open(fs::path(path), std::ios::binary | std::ios::in | std::ios::out);
        if (!f_) {
            error = "cannot open " + path + " to append";
            return false;
        }
        uint8_t bytes[kConversationDiskBinHeader];
        f_.read(reinterpret_cast<char*>(bytes), (std::streamsize) kConversationDiskBinHeader);
        ConversationDiskBinHeader found;
        // the id is not compared: the start-up scan renumbers the entries it takes in, so the header's id is the one the
        // write used, not the tier's.  The file's name and the stage bind it.
        if (!f_ || !conversation_disk_bin_header_read(bytes, kConversationDiskBinHeader, found) ||
            found.model != header.model || found.config != header.config || found.stage != header.stage) {
            error = "the header of " + path + " names another file";
            return false;
        }
        f_.seekp((std::streamoff) end);
        if (!f_) {
            error = "cannot seek in " + path;
            return false;
        }
        at_ = end;
        return true;
    }
    bool write(const void* p, size_t n) {
        if (!n) return true;
        f_.write(reinterpret_cast<const char*>(p), (std::streamsize) n);
        if (!f_) return false;
        at_ += n;
        return true;
    }
    // closes and cuts a torn tail past the last indexed block
    bool close(std::string& error) {
        f_.close();
        std::error_code ec;
        fs::resize_file(fs::path(path_), at_, ec);
        if (ec) {
            error = "cannot cut " + path_ + " back: " + ec.message();
            return false;
        }
        return true;
    }
    uint64_t at() const { return at_; }
private:
    std::fstream f_;
    std::string path_;
    uint64_t at_ = 0;
};

// Writes one block's bytes and hashes them: the record's 128 bits are the two SessionHasher runs of what was written.
class BlockOut {
public:
    explicit BlockOut(BinFile& f) : f_(f), offset_(f.at()), lo_(kBlockSeedLo), hi_(kBlockSeedHi) {}
    void u64(uint64_t v) { raw(&v, 8); }
    void i64(int64_t v) { raw(&v, 8); }
    void raw(const void* p, size_t n) {
        if (!ok_ || !n) return;
        if (!f_.write(p, n)) {
            ok_ = false;
            return;
        }
        lo_.update(p, n);
        hi_.update(p, n);
        n_ += n;
    }
    bool buffer(const ConversationBuffer& b, size_t offset, size_t n) {
        if (!n) return ok_;
        b.visit(offset, n, [&](const uint8_t* p, size_t c, size_t) {
            raw(p, c);
            return ok_;
        });
        return ok_;
    }
    bool ok() const { return ok_; }
    ConversationDiskBlock done(uint32_t kind, int64_t start, int64_t end) const {
        ConversationDiskBlock b;
        b.kind = kind;
        b.offset = offset_;
        b.size = n_;
        b.hash_lo = lo_.digest();
        b.hash_hi = hi_.digest();
        b.tokens_start = start;
        b.tokens_end = end;
        return b;
    }
private:
    BinFile& f_;
    uint64_t offset_;
    uint64_t n_ = 0;
    SessionHasher lo_, hi_;
    bool ok_ = true;
};

// A kv block's bytes: for every layer of the stage its K/V header, then the five parts of the range [start,end).  The
// indexer's completed rows are the ones the range completes (rows [start/page, end/page)).
bool write_kv_payload(BlockOut& out, const SavedConversation& img, const KvPlan& plan, int64_t start, int64_t end) {
    out.u64(img.kv.size());
    const size_t cells = (size_t) (end - start);
    for (size_t l = 0; l < img.kv.size() && out.ok(); ++l) {
        const ConversationKv& kv = img.kv[l];
        out.i64(kv.format);
        out.i64(kv.heads);
        out.i64(kv.head_dim);
        out.i64(kv.page_size);
        out.i64(kv.idx_dim);
        out.u64(plan.row[l]);
        const ConversationBuffer* parts[4] = {&kv.k, &kv.v, &kv.k_scale, &kv.v_scale};
        for (size_t p = 0; p < 4; ++p) {
            const size_t stride = plan.stride[l][p];
            out.u64(stride * cells);
            if (stride) out.buffer(*parts[p], stride * (size_t) start, stride * cells);
        }
        const size_t rows = plan.row[l] ? (size_t) ((end - start) / plan.page) : 0;
        out.u64(rows * plan.row[l]);
        if (rows) out.buffer(kv.pooled, (size_t) (start / plan.page) * plan.row[l], rows * plan.row[l]);
    }
    return out.ok();
}

uint64_t kv_payload_size(const SavedConversation& img, const KvPlan& plan, int64_t start, int64_t end) {
    const size_t cells = (size_t) (end - start);
    uint64_t n = 8;
    for (size_t l = 0; l < img.kv.size(); ++l) {
        n += 6 * 8;
        for (size_t p = 0; p < 4; ++p) n += 8 + plan.stride[l][p] * cells;
        const size_t rows = plan.row[l] ? (size_t) ((end - start) / plan.page) : 0;
        n += 8 + rows * plan.row[l];
    }
    return n;
}

// A state block's bytes: the checkpoint's running-state arrays.  Its ids and images are the index's key, and `used`
// and `pinned` are in the block record, so neither is here.
bool write_state_payload(BlockOut& out, const ConversationCheckpoint& c) {
    for (const auto* v : {&c.gdn, &c.ple, &c.tails, &c.dead, &c.block_pos}) {
        out.u64(v->size());
        out.raw(v->data(), v->size());
    }
    return out.ok();
}

uint64_t state_payload_size(const ConversationCheckpoint& c) {
    return 5 * 8 + c.gdn.size() + c.ple.size() + c.tails.size() + c.dead.size() + c.block_pos.size();
}

// The v2 index's size for an entry: its identity, keys and table, as index_v2_bytes writes them (a record is 69
// bytes; the few bytes of slack cover the counts).
uint64_t index_estimate(const ConversationDiskEntry& e) {
    uint64_t n = 128 + e.stages.size() * 24;
    auto key = [&](const ConversationCheckpoint& c) { n += 16 + c.ids.size() * 4 + c.imgs.size() * 16; };
    key(e.live);
    for (const auto& c : e.checkpoints) key(c);
    for (const auto& s : e.stages) n += 12 + s.file.size() + s.blocks.size() * 72;
    return n;
}

// A block's bytes, read whole and checked against the record's 128 bits.
bool read_block(const std::string& path, const ConversationDiskBlock& b, std::vector<uint8_t>& out,
                std::string& error) {
    if (b.size > (uint64_t) SIZE_MAX) {
        error = "a block is larger than this build can address";
        return false;
    }
    out.assign((size_t) b.size, 0);
    std::ifstream f(fs::path(path), std::ios::binary);
    if (!f) {
        error = "cannot open " + path;
        return false;
    }
    f.seekg((std::streamoff) b.offset);
    f.read(reinterpret_cast<char*>(out.data()), (std::streamsize) out.size());
    if ((size_t) f.gcount() != out.size()) {
        error = "the block at " + std::to_string(b.offset) + " of " + path + " is cut short";
        return false;
    }
    if (session_hash64(out.data(), out.size(), kBlockSeedLo) != b.hash_lo ||
        session_hash64(out.data(), out.size(), kBlockSeedHi) != b.hash_hi) {
        error = "the block at " + std::to_string(b.offset) + " of " + path + " fails its hash";
        return false;
    }
    return true;
}

// The block payloads' reader: counts before arrays, as the index.
class Bytes {
public:
    explicit Bytes(const std::vector<uint8_t>& b) : b_(b) {}
    bool u64(uint64_t& v) { return raw(&v, 8); }
    bool i64(int64_t& v) { return raw(&v, 8); }
    bool raw(void* p, size_t n) {
        if (n > b_.size() - at_) return false;
        std::memcpy(p, b_.data() + at_, n);
        at_ += n;
        return true;
    }
    bool slice(const uint8_t*& p, size_t& n) {
        uint64_t c = 0;
        if (!u64(c) || c > b_.size() - at_) return false;
        p = b_.data() + at_;
        n = (size_t) c;
        at_ += n;
        return true;
    }
    bool done() const { return at_ == b_.size(); }
private:
    const std::vector<uint8_t>& b_;
    size_t at_ = 0;
};

bool read_state_payload(Bytes& in, ConversationCheckpoint& c) {
    std::vector<uint8_t>* parts[5] = {&c.gdn, &c.ple, &c.tails, &c.dead, &c.block_pos};
    for (auto* v : parts) {
        const uint8_t* p = nullptr;
        size_t n = 0;
        if (!in.slice(p, n)) return false;
        if (n) v->assign(p, p + n);
        else v->clear();
    }
    return in.done();
}

// Fills a stage image's K/V buffers from one kv block.  The first block of a stage sizes every layer (its geometry is
// in the payload); later blocks only fill the ranges they cover.  Only the cells the image needs are copied: the
// block may be the one the state's position falls inside, and its bytes past `round_up(tokens, page)` belong to a
// later capture (the image's buffers end there anyway).
bool read_kv_payload(Bytes& in, const ConversationDiskBlock& rec, int64_t tokens, uint32_t layers,
                     SavedConversation& img, std::string& error) {
    uint64_t nlayers = 0;
    if (!in.u64(nlayers) || nlayers != layers) {
        error = "a kv block holds another number of layers";
        return false;
    }
    const size_t range = (size_t) (rec.tokens_end - rec.tokens_start);
    if (!range) {
        error = "a kv block covers no tokens";
        return false;
    }
    const bool fresh = img.kv.empty();
    if (fresh) img.kv.resize((size_t) nlayers);
    for (size_t l = 0; l < (size_t) nlayers; ++l) {
        int64_t format = 0, heads = 0, head_dim = 0, page = 0, idx_dim = 0;
        uint64_t row = 0;
        if (!in.i64(format) || !in.i64(heads) || !in.i64(head_dim) || !in.i64(page) || !in.i64(idx_dim) ||
            !in.u64(row) || heads <= 0 || head_dim <= 0 || page <= 0 || idx_dim < 0 || row > SIZE_MAX) {
            error = "a kv block's layer header is invalid";
            return false;
        }
        ConversationKv& kv = img.kv[l];
        if (fresh) {
            kv.format = (int) format;
            kv.heads = heads;
            kv.head_dim = head_dim;
            kv.page_size = page;
            kv.idx_dim = idx_dim;
            kv.cells = round_up(tokens, page);
            kv.pooled_rows = row ? tokens / page + 1 : 0;
        } else if (kv.format != (int) format || kv.heads != heads || kv.head_dim != head_dim ||
                   kv.page_size != page || kv.idx_dim != idx_dim || (size_t) kv.idx_dim * sizeof(float) != row) {
            error = "the kv blocks of a stage disagree on their layers";
            return false;
        }
        // the cells of this block that belong to the image: up to the last whole page at or below `tokens`
        const size_t cells = (size_t) kv.cells;
        const size_t want = (size_t) std::min<int64_t>((int64_t) range, round_up(tokens, page) - rec.tokens_start);
        if (want > range || want % (size_t) page || (size_t) rec.tokens_start + want > cells) {
            error = "a kv block does not fit the image's extent";
            return false;
        }
        for (size_t p = 0; p < 4; ++p) {
            const uint8_t* pbytes = nullptr;
            size_t n = 0;
            if (!in.slice(pbytes, n) || n % range) {
                error = "a K/V slice is not a whole number of cells";
                return false;
            }
            const size_t stride = n / range;
            ConversationBuffer* buf = p == 0 ? &kv.k : p == 1 ? &kv.v : p == 2 ? &kv.k_scale : &kv.v_scale;
            if (fresh) buf->resize(stride * cells);
            else if (buf->size() != stride * cells) {
                error = "a K/V slice disagrees with the layer's geometry";
                return false;
            }
            const size_t ncopy = stride * want;
            size_t at = 0;
            if (ncopy && (!buf->visit(stride * (size_t) rec.tokens_start, ncopy, [&](uint8_t* d, size_t c, size_t) {
                    std::memcpy(d, pbytes + at, c);
                    at += c;
                    return true;
                }) || at != ncopy)) {
                error = "a K/V slice does not fit its layer";
                return false;
            }
        }
        const uint8_t* prow = nullptr;
        size_t nrow = 0;
        if (!in.slice(prow, nrow)) {
            error = "a kv block ends early";
            return false;
        }
        if (fresh) kv.pooled.resize((size_t) kv.pooled_rows * row);
        if (nrow) {
            const size_t rowsz = row;
            const size_t rows = want / (size_t) page;
            if (!rowsz || nrow % rowsz || (size_t) (rec.tokens_start / page) + rows > (size_t) kv.pooled_rows) {
                error = "a pooled slice does not fit its layer";
                return false;
            }
            size_t at = 0;
            const size_t ncopy = rows * rowsz;
            if (ncopy && !kv.pooled.visit(((size_t) rec.tokens_start / (size_t) page) * rowsz, ncopy,
                                          [&](uint8_t* d, size_t c, size_t) {
                                              std::memcpy(d, prow + at, c);
                                              at += c;
                                              return true;
                                          })) {
                error = "a pooled slice does not fit its layer";
                return false;
            }
        }
    }
    if (!in.done()) {
        error = "a kv block holds bytes past its last layer";
        return false;
    }
    return true;
}

ConversationCheckpoint* state_at(SavedConversation& img, int64_t tokens) {
    if ((int64_t) img.live.ids.size() == tokens) return &img.live;
    for (auto& c : img.checkpoints)
        if ((int64_t) c.ids.size() == tokens) return &c;
    return nullptr;
}

ConversationCheckpoint key_only(const ConversationCheckpoint& c) {
    ConversationCheckpoint k;
    k.ids = c.ids;
    k.imgs = c.imgs;
    return k;
}

// The entry a write appends to: the same stages, layer carves and layer counts; the deepest stored state's tokens
// and images equal to the image's at that length; and every stored block's chained digest recomputed from the image.
// That is the entry a read would have picked, so it is the one worth extending.
bool chain_prefix(const ConversationDiskEntry& e, const std::vector<SavedConversation>& images) {
    if (e.stages.empty() || e.stages.size() != images.size() || e.cvec != images.front().cvec ||
        e.geometry != images.front().geometry || e.tokens <= 0)
        return false;
    for (size_t s = 0; s < images.size(); ++s) {
        const auto& stage = e.stages[s];
        if (stage.layer_lo != images[s].layer_lo || stage.layer_hi != images[s].layer_hi ||
            stage.layers != images[s].kv.size() || stage.blocks.empty())
            return false;
    }
    const SavedConversation& img = images.front();
    const size_t tokens = img.live.ids.size();
    if ((size_t) e.tokens > tokens) return false;
    auto same_state = [&](const ConversationCheckpoint& key) {
        const size_t n = key.ids.size();
        if (!n || n > tokens || !std::equal(key.ids.begin(), key.ids.end(), img.live.ids.begin())) return false;
        size_t j = 0;
        for (const auto& image : img.live.imgs) {
            if (image.start >= (int64_t) n) break;
            if (j >= key.imgs.size() || !(key.imgs[j] == image)) return false;
            ++j;
        }
        return j == key.imgs.size();
    };
    if (!same_state(e.live)) return false;
    for (const auto& c : e.checkpoints)
        if (!same_state(c)) return false;
    for (const auto& stage : e.stages) {
        uint64_t prefix = 0;
        for (const auto& b : stage.blocks) {
            if (b.tokens_start < 0 || (size_t) b.tokens_start > tokens) return false;
            const uint64_t digest = prefix_digest(img.live.ids, img.live.imgs, (size_t) b.tokens_start, prefix);
            if (digest != b.prefix_digest) return false;
            prefix = digest;
        }
    }
    return true;
}

// The entry's states after this image: the deepest state is `live`, the rest are checkpoints, deepest first.  A state
// the entry already holds at a position keeps its key (the chain check proved the image agrees there).
void merge_keys(ConversationDiskEntry& e, const SavedConversation& img) {
    std::map<int64_t, ConversationCheckpoint> by_position;
    auto add = [&](const ConversationCheckpoint& k) {
        const int64_t at = (int64_t) k.ids.size();
        if (!by_position.count(at)) by_position[at] = key_only(k);
    };
    for (const auto& c : e.checkpoints) add(c);
    add(e.live);
    add(img.live);
    for (const auto& c : img.checkpoints) add(c);
    e.live = by_position.rbegin()->second;
    e.checkpoints.clear();
    for (auto it = std::next(by_position.rbegin()); it != by_position.rend(); ++it)
        e.checkpoints.push_back(it->second);
    e.tokens = (int64_t) e.live.ids.size();
}

// The state positions every stage holds a block for, deepest first, at or below `limit`: what a read can rebuild.
// (A key without a block in every stage cannot be restored: its bytes are not there.)
std::vector<int64_t> state_positions(const ConversationDiskEntry& e, int64_t limit) {
    std::vector<int64_t> out;
    if (e.stages.empty()) return out;
    std::vector<int64_t> keys;
    keys.push_back((int64_t) e.live.ids.size());
    for (const auto& c : e.checkpoints) keys.push_back((int64_t) c.ids.size());
    for (const int64_t at : keys) {
        if (at <= 0 || at > limit) continue;
        bool every_stage = true;
        for (const auto& stage : e.stages) {
            bool found = false;
            for (const auto& b : stage.blocks)
                if (b.kind == ConversationDiskBlock::state && b.tokens_start == at) found = true;
            every_stage = every_stage && found;
            if (!every_stage) break;
        }
        if (every_stage) out.push_back(at);
    }
    std::sort(out.begin(), out.end(), std::greater<int64_t>());
    out.erase(std::unique(out.begin(), out.end()), out.end());
    return out;
}

// The blocks a read of the state at `position` uses, and the bytes they take: the kv blocks the state's extent needs,
// then every state block at or below it (the checkpoints it hands back).
uint64_t read_bytes_of(const ConversationDiskEntry& e, int64_t position) {
    uint64_t total = 0;
    for (const auto& stage : e.stages)
        for (const auto& b : stage.blocks) {
            const bool needed = b.kind == ConversationDiskBlock::kv ? b.tokens_start < position
                                                                    : b.tokens_start <= position;
            if (needed) total += b.size;
        }
    return total;
}

// Reads the entry's blocks back into the image they describe, as of the state at `position`: every stage's image, that
// state as the image's `live`, the states below it as its checkpoints, and its K/V up to the state's extent.  Every
// block read is checked against its 128 bits and against its place in the chained prefix digest; a block that fails
// leaves `error` naming it, and the caller falls back to an earlier state.  `admit` is asked for the blocks' bytes
// first.
bool read_entry_image(const ConversationDiskEntry& e, int64_t position, SavedConversation& out, uint64_t& bytes,
                      const std::function<bool(uint64_t, std::string&)>& admit, std::string& error) {
    out = SavedConversation{};
    bytes = 0;
    if (e.stages.empty()) {
        error = "the entry holds no image";
        return false;
    }
    uint64_t total = read_bytes_of(e, position);
    if (admit && !admit(total, error)) return false;
    auto key_at = [&](int64_t at) -> const ConversationCheckpoint* {
        if ((int64_t) e.live.ids.size() == at) return &e.live;
        for (const auto& c : e.checkpoints)
            if ((int64_t) c.ids.size() == at) return &c;
        return nullptr;
    };
    const ConversationCheckpoint* deepest = key_at(position);
    if (deepest == nullptr) {
        error = "the entry has no key at the state's position " + std::to_string(position);
        return false;
    }
    // the checkpoints every stage can give back: the states below the one read, deepest first
    std::vector<const ConversationCheckpoint*> below;
    for (const int64_t at : state_positions(e, position - 1)) {
        const ConversationCheckpoint* key = key_at(at);
        if (key) below.push_back(key);
    }
    for (size_t s = 0; s < e.stages.size(); ++s) {
        const ConversationDiskStage& stage = e.stages[s];
        SavedConversation& img = s == 0 ? out : out.stage_images.emplace_back();
        img.geometry = e.geometry;
        img.cvec = e.cvec;
        img.layer_lo = stage.layer_lo;
        img.layer_hi = stage.layer_hi;
        img.live = key_only(*deepest);
        for (const auto* c : below) img.checkpoints.push_back(key_only(*c));
        // the blocks in table (append) order: the chained digest is over that order, and the kv blocks of a stage
        // build on the first one's layer geometry
        uint64_t prefix = 0;
        std::vector<uint8_t> block;
        for (const auto& b : stage.blocks) {
            const uint64_t digest = prefix_digest(e.live.ids, e.live.imgs, (size_t) b.tokens_start, prefix);
            const bool needed = b.kind == ConversationDiskBlock::kv ? b.tokens_start < position
                                                                    : b.tokens_start <= position;
            if (needed) {
                const char* kind = b.kind == ConversationDiskBlock::kv ? "kv" : "state";
                if (digest != b.prefix_digest) {
                    error = "the " + std::string(kind) + " block at " + std::to_string(b.tokens_start) + "-" +
                            std::to_string(b.tokens_end) + " tokens of " + stage.file +
                            " is not the block this conversation's index names";
                    return false;
                }
                if (!read_block(stage.file, b, block, error)) return false;
                Bytes in{block};
                if (b.kind == ConversationDiskBlock::kv) {
                    if (!read_kv_payload(in, b, position, stage.layers, img, error)) return false;
                } else {
                    ConversationCheckpoint* c = state_at(img, b.tokens_start);
                    if (c == nullptr) {
                        error = "a state block at " + std::to_string(b.tokens_start) + " tokens has no key";
                        return false;
                    }
                    c->used = b.used;
                    c->pinned = b.pinned;
                    if (!read_state_payload(in, *c)) {
                        error = "a state block at " + std::to_string(b.tokens_start) + " tokens is malformed";
                        return false;
                    }
                }
                bytes += b.size;
            }
            prefix = digest;   // recomputed, not read: a record that is not needed cannot break a longer chain
        }
        // the moving spare indexer row of the state comes from its state block (the kv blocks hold the completed rows
        // only), byte-identical to a capture at that position
        if (!img.kv.empty()) {
            const int64_t page = img.kv.front().page_size;
            for (size_t l = 0; l < img.kv.size(); ++l) {
                if (!img.kv[l].pooled_rows) continue;   // a layer without an indexer (the draft)
                const size_t row = (size_t) img.kv[l].idx_dim * sizeof(float);
                if (img.live.dead.size() < (l + 1) * row) {
                    error = "the state at " + std::to_string(position) + " has no spare indexer row for layer " +
                            std::to_string(l);
                    return false;
                }
                if (!img.kv[l].pooled.visit((size_t) (position / page) * row, row, [&](uint8_t* d, size_t n, size_t) {
                        std::memcpy(d, img.live.dead.data() + l * row, n);
                        return true;
                    })) {
                    error = "the state's spare indexer row does not fit its layer";
                    return false;
                }
            }
        }
        bytes += kConversationDiskBinHeader;
    }
    return true;
}

double mib(uint64_t bytes) { return double(bytes) / (1024.0 * 1024.0); }
double mbps(uint64_t bytes, double ms) { return ms > 0 ? double(bytes) / 1e6 / (ms / 1000.0) : 0.0; }
double ms_since(std::chrono::steady_clock::time_point t0) {
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
}

#if defined(__GNUC__)
std::string fmt(const char* f, ...) __attribute__((format(printf, 1, 2)));
#endif
std::string fmt(const char* f, ...) {
    char buf[512];
    va_list ap;
    va_start(ap, f);
    std::vsnprintf(buf, sizeof buf, f, ap);
    va_end(ap);
    return buf;
}

}  // namespace

// ---- container v2 (conversation_disk.hpp): the `.bin` header, the index's writer and reader.

void conversation_disk_bin_header_write(uint8_t* out, const ConversationDiskBinHeader& header) {
    std::memset(out, 0, kConversationDiskBinHeader);
    const uint32_t version = kBinVersion, size = (uint32_t) kConversationDiskBinHeader;
    std::memcpy(out, &kBinMagic, 8);
    std::memcpy(out + 8, &version, 4);
    std::memcpy(out + 12, &size, 4);
    std::memcpy(out + 16, &header.model, 8);
    std::memcpy(out + 24, &header.config, 8);
    std::memcpy(out + 32, &header.id, 8);
    std::memcpy(out + 40, &header.stage, 4);   // bytes 44..63 stay zero (reserved)
}

bool conversation_disk_bin_header_read(const uint8_t* bytes, size_t n, ConversationDiskBinHeader& header) {
    if (!bytes || n < kConversationDiskBinHeader) return false;
    uint64_t magic = 0;
    uint32_t version = 0, size = 0;
    std::memcpy(&magic, bytes, 8);
    std::memcpy(&version, bytes + 8, 4);
    std::memcpy(&size, bytes + 12, 4);
    if (magic != kBinMagic || version != kBinVersion || size != kConversationDiskBinHeader) return false;
    std::memcpy(&header.model, bytes + 16, 8);
    std::memcpy(&header.config, bytes + 24, 8);
    std::memcpy(&header.id, bytes + 32, 8);
    std::memcpy(&header.stage, bytes + 40, 4);
    return true;
}

bool conversation_disk_index_write(const std::string& path, const ConversationDiskEntry& entry,
                                   const SessionFileIdentity& identity, std::string& error) {
    const fs::path target(path);
    const fs::path temp = target.parent_path() / ("." + target.filename().string() + ".tmp");
    IndexWriter out;
    index_v2_bytes(out, entry, identity);
    {
        std::ofstream f(temp, std::ios::binary | std::ios::trunc);
        f.write(out.b.data(), (std::streamsize) out.b.size());
        if (!f) {
            error = "cannot write the index " + temp.string();
            remove_file(temp.string());
            return false;
        }
    }
    std::error_code ec;
    fs::rename(temp, target, ec);   // a reader sees the whole index or none
    if (ec) {
        error = "cannot publish the index: " + ec.message();
        remove_file(temp.string());
        return false;
    }
    return true;
}

bool conversation_disk_index_read(const std::string& path, const SessionFileIdentity& identity,
                                  ConversationDiskEntry& entry) {
    std::ifstream f(fs::path(path), std::ios::binary);
    const std::string b((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    if (b.size() < 8 || std::memcmp(b.data(), &kIndexMagicV2, 8) != 0) return false;   // not a v2 index
    entry = ConversationDiskEntry{};
    if (!read_index_v2(b, identity, entry)) {
        entry = ConversationDiskEntry{};
        return false;
    }
    entry.index = path;
    return true;
}

ConversationDiskTier::ConversationDiskTier(ConversationDiskOptions options) : options_(std::move(options)) {
    std::random_device rd;
    nonce_ = ((uint64_t) rd() << 32) ^ rd() ^ (uint64_t) std::chrono::system_clock::now().time_since_epoch().count();
}

ConversationDiskTier::~ConversationDiskTier() {
    if (open_ && options_.persist) wait_idle();   // what is queued belongs to the next start
    size_t dropped = 0;
    {
        std::lock_guard<std::mutex> lock(m_);
        stop_ = true;
        dropped = queue_.size();
        queue_.clear();   // not written: dropped, as the RAM cache would have
    }
    cv_.notify_all();
    if (thread_.joinable()) thread_.join();
    std::lock_guard<std::mutex> lock(m_);
    if (!open_) return;
    if (options_.persist) {
        log(fmt("end: %zu conversations kept on the disk for the next start (%.0f MiB)", entries_.size(), mib(bytes_)));
        return;
    }
    if (dropped || !entries_.empty())
        log(fmt("end: %zu queued dropped, %zu conversations removed from the disk", dropped, entries_.size()));
    while (!entries_.empty()) remove_locked(0, true);
}

SessionFileIdentity ConversationDiskTier::file_identity() const {
    return options_.persist ? options_.identity : SessionFileIdentity{nonce_, kTierConfig};
}

bool ConversationDiskTier::write_index_v2(Entry& e, std::string& error) const {
    fs::path path;
    if (!e.index.empty()) {
        path = fs::path(e.index);   // an entry taken in from an earlier run keeps its index file
    } else {
        char name[96];
        std::snprintf(name, sizeof name, "%s%016llx-%llu.idx", file_prefix(), (unsigned long long) nonce_,
                      (unsigned long long) e.id);
        path = fs::path(options_.dir) / name;
    }
    e.written_s = std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
    if (!conversation_disk_index_write(path.string(), e, file_identity(), error)) return false;
    e.index = path.string();
    return true;
}

bool ConversationDiskTier::check_files(const Entry& e) const {
    // A restorable entry has a state block in every stage: a crash between the first K/V append and the first state
    // append leaves blocks nobody can restore from.
    for (const auto& stage : e.stages) {
        bool have_state = false;
        for (const auto& block : stage.blocks)
            if (block.kind == ConversationDiskBlock::state) have_state = true;
        if (!have_state) return false;
    }
    // Every stage's `.bin`: its header (this model and settings, this entry, this stage) and at least the bytes the
    // table names.  A longer file is a torn tail past the last indexed block: it stays and is ignored until the next
    // append writes over it.
    const SessionFileIdentity want = file_identity();
    uint64_t entry_id = 0;
    bool have_id = false;
    for (size_t s = 0; s < e.stages.size(); ++s) {
        const auto& stage = e.stages[s];
        std::error_code ec;
        const fs::path path(stage.file);
        if (!fs::is_regular_file(path, ec)) return false;
        uint8_t bytes[kConversationDiskBinHeader];
        {
            std::ifstream f(path, std::ios::binary);
            f.read(reinterpret_cast<char*>(bytes), (std::streamsize) sizeof bytes);
            if (!f) return false;
        }
        ConversationDiskBinHeader header;
        if (!conversation_disk_bin_header_read(bytes, sizeof bytes, header)) return false;
        if (header.model != want.model || header.config != want.config) return false;
        uint64_t file_id = 0;
        uint32_t file_stage = 0;
        if (name_ids(stage.file, file_id, file_stage)) {
            if (header.id != file_id || header.stage != file_stage) return false;   // another entry's or stage's file
        }
        if (header.stage != (uint32_t) s) return false;
        if (!have_id) {
            entry_id = header.id;
            have_id = true;
        } else if (header.id != entry_id) {
            return false;   // the stages of one entry are written under one id
        }
        uint64_t end = kConversationDiskBinHeader;
        for (const auto& block : stage.blocks) end = std::max(end, block.offset + block.size);
        const auto size = fs::file_size(path, ec);
        if (ec || size < end) return false;   // the index outlived its data
    }
    return true;
}

bool ConversationDiskTier::read_index(const std::string& path, Entry& e) const {
    std::ifstream f(fs::path(path), std::ios::binary);
    const std::string b((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    uint64_t magic = 0;
    if (b.size() >= sizeof magic) std::memcpy(&magic, b.data(), sizeof magic);
    if (magic != kIndexMagicV2) return false;   // a v1 index ("STRCIDX1") or not an index: the scan removes it
    if (!read_index_v2(b, file_identity(), e)) return false;
    for (auto& stage : e.stages) stage.file = (fs::path(options_.dir) / fs::path(stage.file).filename()).string();
    if (!check_files(e)) return false;
    e.index = path;
    return true;
}

std::string ConversationDiskTier::scan_folder(size_t& stale, std::error_code& ec) {
    std::vector<Entry> found;
    std::vector<std::string> others;
    size_t refused = 0;
    for (fs::directory_iterator it(fs::path(options_.dir), ec), end; !ec && it != end; it.increment(ec)) {
        if (!it->is_regular_file(ec) || !tier_file(it->path().filename().string())) continue;
        Entry e;
        const bool index = it->path().extension() == ".idx";
        const bool taken = index && read_index(it->path().string(), e);
        if (taken) found.push_back(std::move(e));
        else others.push_back(it->path().string());
        refused += index && !taken;
    }
    // the files of the entries taken in stay; the rest goes (refused indexes and their files, interrupted writes)
    std::set<std::string> kept;
    for (const auto& e : found) {
        const std::vector<std::string> owned = files_of(e);
        kept.insert(owned.begin(), owned.end());
    }
    for (const auto& p : others)
        if (!kept.count(p)) { remove_file(p); ++stale; }
    std::sort(found.begin(), found.end(), [](const Entry& a, const Entry& b) { return lru_s(a) < lru_s(b); });
    for (auto& e : found) {
        e.id = ++next_id_;
        bytes_ += e.bytes;
        entries_.push_back(std::move(e));
    }
    size_t over = 0;
    while (!entries_.empty() && bytes_ > options_.budget_bytes) {
        const size_t victim = lru_victim(entries_, 0);
        if (victim == entries_.size()) break;
        remove_locked(victim, true);
        ++over;
    }
    return fmt("persistent (budget only): %zu conversations from earlier runs (%.0f MiB), %zu refused (a v1 index, "
               "another model, engine version or settings, a missing state block, a file the table outgrew), %zu over "
               "the budget removed", entries_.size(), mib(bytes_), refused, over);
}

void ConversationDiskTier::log(const std::string& line) const {
    if (options_.log) options_.log(line);
    else std::fprintf(stderr, "strata serve: conversation disk: %s\n", line.c_str());
}

bool ConversationDiskTier::open(std::string& error) {
    open_ = false;
    if (options_.dir.empty() || options_.budget_bytes == 0) return true;   // off: nothing to open
    std::error_code ec;
    const fs::path dir(options_.dir);
    fs::create_directories(dir, ec);
    if (ec || !fs::is_directory(dir, ec)) {
        error = "cannot create the folder " + options_.dir + (ec ? ": " + ec.message() : std::string());
        return false;
    }
#ifndef _WIN32
    fs::permissions(dir, fs::perms::owner_all, fs::perm_options::replace, ec);   // the files hold conversations
    ec.clear();
#endif
    size_t stale = 0;
    const std::string taken_in = scan_folder(stale, ec);
    if (ec) {
        error = "cannot list the folder " + options_.dir + ": " + ec.message();
        return false;
    }
    open_ = true;
    thread_ = std::thread([this] { writer(); });
    const std::string kept = options_.persist ? taken_in : "removed at the end";
    log(fmt("on: %s, budget %.0f MiB, %s; %zu stale files removed", options_.dir.c_str(), mib(options_.budget_bytes),
            kept.c_str(), stale));
    return true;
}

void ConversationDiskTier::remove_locked(size_t index, bool delete_files) {
    Entry& e = entries_.at(index);
    if (!e.index.empty()) remove_file(e.index);   // first, always: an entry gone from the list is not taken in again
    if (delete_files)
        for (const auto& f : files_of(e)) remove_file(f);
    bytes_ -= e.bytes;
    entries_.erase(entries_.begin() + (std::ptrdiff_t) index);
}

bool ConversationDiskTier::put(SavedConversation&& image) {
    if (!open_) return false;
    Job job;
    job.image = std::move(image);
    job.entry.live = key_of(job.image.live);
    for (const auto& c : job.image.checkpoints) job.entry.checkpoints.push_back(key_of(c));
    job.entry.cvec = job.image.cvec;
    job.entry.bytes = job.image.bytes();
    const size_t tokens = job.entry.live.ids.size();
    if (job.entry.bytes > options_.budget_bytes) {
        log(fmt("dropped %zu tokens: %.0f MiB is over the disk budget", tokens, mib(job.entry.bytes)));
        std::lock_guard<std::mutex> lock(m_);
        ++stats_.write_failed;
        return false;
    }
    {
        std::lock_guard<std::mutex> lock(m_);
        job.entry.id = ++next_id_;
        ++stats_.queued;
    }
    size_t depth = 0;
    uint64_t queued_bytes = 0;
    const uint64_t estimate = job.entry.bytes;
    {
        std::lock_guard<std::mutex> lock(m_);
        queue_.push_back(std::move(job));
        depth = queue_.size();
        for (const auto& j : queue_) queued_bytes += j.entry.bytes;
    }
    cv_.notify_all();
    log(fmt("queued %zu tokens (%.0f MiB in RAM until written; queue %zu, %.0f MiB)", tokens, mib(estimate), depth,
            mib(queued_bytes)));
    return true;
}

void ConversationDiskTier::writer() {
#if defined(__linux__)
    // below the serving threads: the write is background work, decode and prompt reading come first
    setpriority(PRIO_PROCESS, (id_t) syscall(SYS_gettid), 10);
#endif
    for (;;) {
        Job job;
        {
            std::unique_lock<std::mutex> lock(m_);
            cv_.wait(lock, [&] { return stop_ || !queue_.empty(); });
            if (stop_) return;
            job = std::move(queue_.front());
            queue_.pop_front();
            writing_ = true;
            writing_entry_ = job.entry;   // best() still sees it; take() waits for its file
        }
        write(job);
        {
            std::lock_guard<std::mutex> lock(m_);
            writing_ = false;
            writing_entry_ = Entry{};
        }
        cv_.notify_all();
    }
}

void ConversationDiskTier::write(Job& job) {
    if (options_.before_write) options_.before_write();
    const size_t tokens = job.entry.live.ids.size();
    const auto t0 = std::chrono::steady_clock::now();
    // the image's stages: the first stage's, then each later stage's (the writer takes no nested images)
    std::vector<SavedConversation> images;
    images.push_back(std::move(job.image));
    for (auto& stage : images.front().stage_images) images.push_back(std::move(stage));
    images.front().stage_images.clear();
    // STRATA_PARK_ASYNC: the park that produced these images may still be copying its K/V into them; the request
    // thread cannot wait (that would undo the asynchronous park), so the writer does, once per image, before it
    // reads a buffer below
    if (options_.join)
        for (const auto& image : images) options_.join(image);

    // 1. the entry to append to: the longest chain that is a prefix of this image (the one a read would pick)
    Entry updated;
    uint64_t target = 0;
    {
        std::lock_guard<std::mutex> lock(m_);
        for (const auto& e : entries_) {
            if (!chain_prefix(e, images)) continue;
            if (!target || e.tokens > updated.tokens) {
                target = e.id;
                updated = e;
            }
        }
        if (!target) {
            updated = Entry{};
            updated.id = ++next_id_;
        }
    }
    if (!target) {
        updated.cvec = images.front().cvec;
        updated.geometry = images.front().geometry;
        updated.tokens = (int64_t) images.front().live.ids.size();
        updated.live = key_of(images.front().live);
        for (const auto& c : images.front().checkpoints) updated.checkpoints.push_back(key_of(c));
        updated.stages.resize(images.size());
        for (size_t s = 0; s < images.size(); ++s) {
            updated.stages[s].layer_lo = images[s].layer_lo;
            updated.stages[s].layer_hi = images[s].layer_hi;
            updated.stages[s].layers = (uint32_t) images[s].kv.size();
        }
        updated.bytes = 0;
    } else {
        merge_keys(updated, images.front());
    }

    // 2. what the write will add: the blocks this image has and the entry does not (sizes only, before anything is
    // written, so the budget can make room first)
    std::string error;
    std::vector<KvPlan> plans(images.size());
    uint64_t need = 0;
    size_t new_blocks = 0;
    std::vector<std::vector<std::pair<int64_t, int64_t>>> ranges(images.size());   // per stage: the new kv ranges
    std::vector<std::vector<const ConversationCheckpoint*>> states(images.size());
    for (size_t s = 0; s < images.size() && error.empty(); ++s) {
        const SavedConversation& img = images[s];
        if (!kv_plan(img, plans[s], error)) break;
        ConversationDiskStage& stage = updated.stages[s];
        if (plans[s].page <= 0) {
            if (!img.kv.empty()) {
                error = "the image has no K/V page size";
                break;
            }
        } else {
            int64_t last = 0;
            for (const auto& b : stage.blocks)
                if (b.kind == ConversationDiskBlock::kv) last = std::max(last, b.tokens_end);
            const int64_t page = plans[s].page;
            const int64_t piece = std::max<int64_t>(page, (kBlockTokens / page) * page);
            for (int64_t start = last; start < plans[s].limit; start += piece) {
                const int64_t end = std::min<int64_t>(start + piece, plans[s].limit);
                ranges[s].push_back({start, end});
                need += kv_payload_size(img, plans[s], start, end);
                ++new_blocks;
            }
        }
        states[s].push_back(&img.live);
        for (const auto& c : img.checkpoints) states[s].push_back(&c);
        for (const auto* c : states[s]) {
            const int64_t at = (int64_t) c->ids.size();
            bool held = false;
            for (const auto& b : stage.blocks)
                if (b.kind == ConversationDiskBlock::state && b.tokens_start == at) held = true;
            if (!held) {
                need += state_payload_size(*c);
                ++new_blocks;
            }
        }
        if (stage.blocks.empty()) need += kConversationDiskBinHeader;
    }
    need += index_estimate(updated) + 72 * new_blocks;   // the v2 index, rewritten whole on every publish

    // 3. the budget: whole conversations, the least recently used first, never the entry being extended or one a read
    // holds
    size_t made_room = 0;
    if (error.empty()) {
        std::lock_guard<std::mutex> lock(m_);
        while (bytes_ + need > options_.budget_bytes) {
            const size_t victim = lru_victim(entries_, target);
            if (victim == entries_.size()) break;   // nothing removable is left (the target, or a read holds it)
            remove_locked(victim, true);
            ++made_room;
        }
        stats_.evicted += made_room;
    }

    // 4. append the blocks: kv first, then the states this entry does not hold yet
    const SessionFileIdentity fid = file_identity();
    std::vector<std::string> created;   // this write's own files, removed when it fails before the index is published
    size_t blocks_written = 0;
    for (size_t s = 0; s < images.size() && error.empty(); ++s) {
        const SavedConversation& img = images[s];
        ConversationDiskStage& stage = updated.stages[s];
        ConversationDiskBinHeader header;
        header.model = fid.model;
        header.config = fid.config;
        header.id = updated.id;
        header.stage = (uint32_t) s;
        BinFile file;
        if (stage.blocks.empty()) {
            char name[96];
            std::snprintf(name, sizeof name, "%s%016llx-%llu-%zu.bin", file_prefix(), (unsigned long long) nonce_,
                          (unsigned long long) updated.id, s);
            stage.file = (fs::path(options_.dir) / name).string();
            created.push_back(stage.file);
            if (!file.open_new(stage.file, header, error)) break;
        } else {
            const auto& last = stage.blocks.back();
            if (!file.open_append(stage.file, header, last.offset + last.size, error)) break;
        }
        uint64_t prefix = stage.blocks.empty() ? 0 : stage.blocks.back().prefix_digest;
        for (const auto& r : ranges[s]) {
            BlockOut out(file);
            if (!write_kv_payload(out, img, plans[s], r.first, r.second)) {
                error = "the K/V of " + std::to_string(r.first) + "-" + std::to_string(r.second) +
                        " tokens could not be written";
                break;
            }
            ConversationDiskBlock rec = out.done(ConversationDiskBlock::kv, r.first, r.second);
            rec.prefix_digest = prefix_digest(img.live.ids, img.live.imgs, (size_t) r.first, prefix);
            prefix = rec.prefix_digest;
            stage.blocks.push_back(rec);
            ++blocks_written;
        }
        for (const auto* c : states[s]) {
            if (!error.empty()) break;
            const int64_t at = (int64_t) c->ids.size();
            bool held = false;
            for (const auto& b : stage.blocks)
                if (b.kind == ConversationDiskBlock::state && b.tokens_start == at) held = true;
            if (held) continue;
            BlockOut out(file);
            if (!write_state_payload(out, *c)) {
                error = "the state at " + std::to_string(at) + " tokens could not be written";
                break;
            }
            ConversationDiskBlock rec = out.done(ConversationDiskBlock::state, at, at);
            rec.prefix_digest = prefix_digest(img.live.ids, img.live.imgs, (size_t) at, prefix);
            rec.used = c->used;
            rec.pinned = c->pinned;
            prefix = rec.prefix_digest;
            stage.blocks.push_back(rec);
            ++blocks_written;
        }
        if (error.empty() && !file.close(error)) break;
    }

    // 5. publish the index: the blocks are on disk, the index names them
    uint64_t bin_bytes = 0, index_bytes = 0;
    if (error.empty()) {
        for (const auto& stage : updated.stages) {
            bin_bytes += kConversationDiskBinHeader;
            for (const auto& b : stage.blocks) bin_bytes += b.size;
        }
        updated.bytes = bin_bytes;
        if (!write_index_v2(updated, error)) {
            for (const auto& f : created) remove_file(f);
            created.clear();
        } else {
            std::error_code ec;
            index_bytes = fs::file_size(updated.index, ec);
            if (ec) index_bytes = 0;   // a size the folder will not give back is not worth failing the write
            updated.bytes += index_bytes;
        }
    }
    job.image = SavedConversation{};   // the RAM goes back now, written or not
    const double ms = ms_since(t0);
    if (!error.empty()) {
        for (const auto& f : created) remove_file(f);
        {
            std::lock_guard<std::mutex> lock(m_);
            ++stats_.write_failed;
        }
        images.clear();
        log(fmt("dropped %zu tokens: the write failed after %.0f ms (%s)", tokens, ms, error.c_str()));
        return;
    }

    // 6. publish it in memory and remove the entries this image supersedes (a shorter state of the same conversation,
    // which a read never picks again)
    const uint64_t new_bytes = updated.bytes;
    const uint64_t new_id = updated.id;
    const int64_t new_tokens = updated.tokens;
    const size_t nstages = updated.stages.size();
    size_t on_disk = 0;
    size_t superseded = 0;
    uint64_t disk_bytes = 0;
    {
        std::lock_guard<std::mutex> lock(m_);
        bool installed = false;
        for (size_t i = 0; i < entries_.size(); ++i)
            if (entries_[i].id == new_id) {
                bytes_ -= entries_[i].bytes;
                const size_t held = entries_[i].readers;          // a read of the older files keeps its protection
                const int64_t was_damaged = entries_[i].verified;
                const int64_t now_damaged = updated.verified;
                entries_[i] = std::move(updated);
                entries_[i].readers = held;
                entries_[i].verified = (was_damaged && now_damaged) ? std::min(was_damaged, now_damaged)
                                                                    : (was_damaged ? was_damaged : now_damaged);
                bytes_ += new_bytes;
                installed = true;
                break;
            }
        if (!installed) {
            entries_.push_back(std::move(updated));
            bytes_ += new_bytes;
        }
        for (size_t i = entries_.size(); i-- > 0;) {
            if (entries_[i].id == new_id || entries_[i].tokens >= new_tokens) continue;
            if (entries_[i].readers > 0) continue;   // a read is using it: the next publish removes it
            if (chain_prefix(entries_[i], images)) {
                remove_locked(i, true);
                ++superseded;
            }
        }
        ++stats_.written;
        stats_.bytes_written += bin_bytes;
        stats_.write_ms += ms;
        on_disk = entries_.size();
        disk_bytes = bytes_;
    }
    images.clear();
    log(fmt("wrote %zu tokens%s: %.0f MiB in %zu blocks, %zu file%s, %.0f ms (%.0f MB/s); on disk %zu, %.0f MiB; "
            "%zu removed for the budget, %zu superseded", tokens, target ? " (appended)" : "", mib(bin_bytes),
            blocks_written, nstages, nstages == 1 ? "" : "s", ms, mbps(bin_bytes, ms), on_disk, mib(disk_bytes),
            made_room, superseded));
    (void) index_bytes;
}


bool ConversationDiskTier::take(uint64_t id, int64_t tokens, SavedConversation& out, std::string& error,
                                const std::function<bool(uint64_t, std::string&)>& admit) {
    out = SavedConversation{};
    const auto t0 = std::chrono::steady_clock::now();
    std::unique_lock<std::mutex> lock(m_);
    if (!open_) { error = "the disk tier is off"; return false; }
    // still queued: back from RAM, no disk at all
    for (size_t i = 0; i < queue_.size(); ++i)
        if (queue_[i].entry.id == id) {
            const uint64_t b = queue_[i].entry.bytes;
            out = std::move(queue_[i].image);
            queue_.erase(queue_.begin() + (std::ptrdiff_t) i);
            ++stats_.ram_hits;
            stats_.tokens_back += (uint64_t) std::max<int64_t>(tokens, 0);
            lock.unlock();
            log(fmt("RAM hit: %lld tokens back from the write queue (%.0f MiB, no disk) in %.1f ms",
                    (long long) tokens, mib(b), ms_since(t0)));
            log(summary());
            return true;
        }
    // being written: its file is complete when the write ends
    double waited = 0;
    if (writing_ && writing_entry_.id == id) {
        const auto w0 = std::chrono::steady_clock::now();
        cv_.wait(lock, [&] { return !(writing_ && writing_entry_.id == id); });
        waited = ms_since(w0);
    }
    size_t index = entries_.size();
    for (size_t i = 0; i < entries_.size(); ++i)
        if (entries_[i].id == id) index = i;
    if (index == entries_.size()) {
        ++stats_.read_failed;
        error = "the entry is gone (its write failed, or the budget removed it)";
        return false;
    }
    // the entry stays on the disk: its table is copied out (a few hundred bytes), the reader's stamp taken, and its
    // files kept for this read - the writer leaves an entry a read holds until its next publish
    const Entry want = entries_[index];
    ++entries_[index].readers;
    entries_[index].last_used_s = std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
    lock.unlock();
    uint64_t bytes = 0;
    const size_t nfiles = want.stages.size();
    // the deepest state the match can rebuild, then the ones before it when a block is damaged
    const int64_t claimed = std::min<int64_t>(tokens > 0 ? tokens : want.tokens, want.tokens);
    const std::vector<int64_t> positions = state_positions(want, claimed);
    int64_t read_at = 0;
    if (positions.empty()) {
        error = "the entry has no restorable state at or below " + std::to_string(claimed) + " tokens";
    } else {
        // the RAM preflight once, for the largest attempt (a fallback reads less)
        uint64_t need = 0;
        for (const int64_t at : positions) need = std::max(need, read_bytes_of(want, at));
        if (admit && !admit(need, error)) {
            error = error.empty() ? "not enough RAM" : error;   // nothing was read; the entry stays
        } else {
            for (const int64_t at : positions) {
                std::string why;
                if (read_entry_image(want, at, out, bytes, {}, why)) {
                    read_at = at;
                    error.clear();
                    break;
                }
                out = SavedConversation{};
                error = why;   // the last failure is the one reported
                log(fmt("read of %lld tokens: the state at %lld could not be read (%s)", (long long) claimed,
                        (long long) at, why.c_str()));
            }
        }
    }
    const double ms = ms_since(t0);
    lock.lock();
    for (auto& e : entries_)
        if (e.id == id) {
            if (e.readers) --e.readers;
            if (read_at > 0 && read_at < (int64_t) e.live.ids.size())   // a damaged chain: best() claims only this
                e.verified = e.verified ? std::min(e.verified, read_at) : read_at;
            break;
        }
    cv_.notify_all();   // clear() waits for the last reader
    if (read_at <= 0) {
        ++stats_.read_failed;
        if (error.empty()) error = "the entry could not be read";
        lock.unlock();
        out = SavedConversation{};
        log(fmt("read of %lld tokens failed after %.0f ms (%s)", (long long) claimed, ms, error.c_str()));
        return false;
    }
    ++stats_.disk_hits;
    if (read_at < claimed) ++stats_.fallbacks;
    stats_.bytes_read += bytes;
    stats_.read_ms += ms - waited;
    stats_.tokens_back += (uint64_t) std::max<int64_t>(read_at, 0);
    lock.unlock();
    log(fmt("disk hit: %lld tokens, %.0f MiB in %zu file%s, %.0f ms (%.0f MB/s)%s%s", (long long) read_at, mib(bytes),
            nfiles, nfiles == 1 ? "" : "s", ms - waited, mbps(bytes, ms - waited),
            read_at < claimed ? fmt(", from the state at %lld (the deeper one is damaged)", (long long) read_at).c_str()
                              : "",
            waited > 0 ? fmt(", after %.0f ms waiting for its write", waited).c_str() : ""));
    log(summary());
    return true;
}

bool ConversationDiskTier::pending() const {
    std::lock_guard<std::mutex> lock(m_);
    return !queue_.empty() || writing_;
}

double ConversationDiskTier::wait_idle() {
    std::unique_lock<std::mutex> lock(m_);
    if (queue_.empty() && !writing_) return 0;
    const auto t0 = std::chrono::steady_clock::now();
    const size_t depth = queue_.size() + (writing_ ? 1 : 0);
    cv_.wait(lock, [&] { return queue_.empty() && !writing_; });
    const double ms = ms_since(t0);
    ++stats_.waits;
    stats_.wait_ms += ms;
    lock.unlock();
    log(fmt("waited %.0f ms for %zu queued write%s (their RAM was needed)", ms, depth, depth == 1 ? "" : "s"));
    return ms;
}

size_t ConversationDiskTier::size() const {
    std::lock_guard<std::mutex> lock(m_);
    return entries_.size();
}

uint64_t ConversationDiskTier::bytes() const {
    std::lock_guard<std::mutex> lock(m_);
    return bytes_;
}

ConversationDiskStats ConversationDiskTier::stats() const {
    std::lock_guard<std::mutex> lock(m_);
    return stats_;
}

size_t ConversationDiskTier::clear() {
    std::unique_lock<std::mutex> lock(m_);
    const size_t queued = queue_.size();
    queue_.clear();
    // a read holds its entry's files: it finishes first (its fallback would otherwise read deleted blocks)
    auto readers = [&] {
        size_t n = 0;
        for (const auto& e : entries_) n += e.readers;
        return n;
    };
    cv_.wait(lock, [&] { return !writing_ && readers() == 0; });
    const size_t n = queued + entries_.size();
    while (!entries_.empty()) remove_locked(0, true);
    lock.unlock();
    if (n) log(fmt("removed all %zu conversations (#879)", n));
    return n;
}

std::string ConversationDiskTier::summary() const {
    std::lock_guard<std::mutex> lock(m_);
    const auto& s = stats_;
    return fmt("totals: %zu queued, %zu written (%.0f MiB, %.0f MB/s), %zu RAM hits, %zu disk hits (%.0f MiB, "
               "%.0f MB/s), %llu prompt tokens given back, %zu waits for RAM (%.0f ms), "
               "%zu removed for the budget, %zu/%zu writes/reads failed, %zu reads fell back on damage; "
               "on disk %zu (%.0f MiB), queue %zu",
               s.queued, s.written, mib(s.bytes_written), mbps(s.bytes_written, s.write_ms), s.ram_hits, s.disk_hits,
               mib(s.bytes_read), mbps(s.bytes_read, s.read_ms), (unsigned long long) s.tokens_back, s.waits,
               s.wait_ms, s.evicted, s.write_failed, s.read_failed, s.fallbacks, entries_.size(), mib(bytes_),
               queue_.size() + (writing_ ? 1 : 0));
}

}  // namespace strata::core
