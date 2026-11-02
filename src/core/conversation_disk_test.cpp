// CPU-only tests for the disk tier of the parked conversations (include/strata/core/conversation_disk.hpp).
// Built with -DSTRATA_BUILD_CONVERSATION_TESTS=ON; no CUDA, no model.
#include "strata/core/conversation_disk.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <future>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

using namespace strata::core;
namespace fs = std::filesystem;

namespace {
int checks = 0;
void check(bool ok, const char* label) {
    ++checks;
    if (!ok) { std::fprintf(stderr, "FAIL: %s\n", label); std::exit(1); }
}

ConversationBuffer pattern(size_t n, uint8_t seed) {
    ConversationBuffer b;
    b.resize(n);
    b.visit(0, n, [&](uint8_t* p, size_t c, size_t at) {
        for (size_t i = 0; i < c; ++i) p[i] = uint8_t((at + i) * 131u + seed);
        return true;
    });
    return b;
}

std::vector<uint8_t> bytes_of(size_t n, uint8_t seed) {
    std::vector<uint8_t> v(n);
    for (size_t i = 0; i < n; ++i) v[i] = uint8_t(i * 7u + seed);
    return v;
}

ConversationCheckpoint checkpoint(size_t tokens, uint8_t seed, int32_t first = 1000) {
    ConversationCheckpoint c;
    for (size_t i = 0; i < tokens; ++i) c.ids.push_back(first + int32_t(i));
    c.gdn = bytes_of(4099, seed);
    c.ple = bytes_of(77, seed + 1);
    c.tails = bytes_of(301, seed + 2);
    c.dead = bytes_of(2 * 128 * 4, seed + 3);   // the moving indexer row, one per layer
    c.block_pos = bytes_of(8, seed + 4);
    c.used = 42 + seed;
    return c;
}

// one stage's image: `tokens` live tokens from `first`, checkpoints at a half and a quarter of them, two K/V layers.
// The K/V follows the engine's layout (conversation_snapshot.cpp): cells rounded up to the page, one indexer row per
// page plus the moving spare row, and that spare row is the live state's `dead` (the store takes it from the state
// block, so a capture keeps it there, byte for byte).
SavedConversation image(size_t tokens, uint8_t seed, int32_t first = 1000, int64_t lo = 0, int64_t hi = 48,
                        int64_t heads = 8) {
    SavedConversation s;
    for (size_t i = 0; i < s.geometry.size(); ++i) s.geometry[i] = int64_t(100 + i);
    s.layer_lo = lo; s.layer_hi = hi;
    s.cvec = false;
    s.live = checkpoint(tokens, seed, first);
    s.checkpoints.push_back(checkpoint(tokens / 2, uint8_t(seed + 9), first));    // the deepest first
    s.checkpoints.push_back(checkpoint(tokens / 4, uint8_t(seed + 19), first));
    const int64_t page = 4;   // the kernels' page, and the indexer's block
    const int64_t cells = int64_t((tokens + page - 1) / page) * page;
    for (int layer = 0; layer < 2; ++layer) {
        ConversationKv kv;
        kv.format = 2; kv.cells = cells; kv.heads = heads; kv.head_dim = 256; kv.page_size = page;
        kv.pooled_rows = int64_t(tokens) / page + 1;
        kv.idx_dim = 128;
        kv.k = pattern(size_t(cells) * size_t(heads) * 256 * 2, uint8_t(seed + layer));
        kv.v = pattern(size_t(cells) * size_t(heads) * 256 * 2, uint8_t(seed + 10));
        kv.k_scale = pattern(size_t(cells) * size_t(heads) * 4 * 2, uint8_t(seed + 20));
        kv.v_scale = pattern(size_t(cells) * size_t(heads) * 4 * 2, uint8_t(seed + 30));
        kv.pooled = pattern(size_t(kv.pooled_rows) * 128 * 4, uint8_t(seed + 40));
        const size_t row = size_t(kv.idx_dim) * sizeof(float);
        size_t done = 0;
        kv.pooled.visit(size_t(kv.pooled_rows - 1) * row, row, [&](uint8_t* p, size_t n, size_t) {
            std::memcpy(p, s.live.dead.data() + size_t(layer) * row + done, n);
            done += n;
            return true;
        });
        s.kv.push_back(std::move(kv));
    }
    return s;
}

// a layer split's image: the first stage's with one later stage's
SavedConversation split_image(size_t tokens, uint8_t seed, int32_t first = 1000) {
    SavedConversation s = image(tokens, seed, first, 0, 26);
    s.stage_images.push_back(image(tokens, uint8_t(seed + 50), first, 26, 48));
    return s;
}

bool same_checkpoint(const ConversationCheckpoint& a, const ConversationCheckpoint& b) {
    return a.ids == b.ids && a.imgs == b.imgs && a.gdn == b.gdn && a.ple == b.ple && a.tails == b.tails &&
           a.dead == b.dead && a.block_pos == b.block_pos && a.used == b.used;
}

bool same_kv(const ConversationKv& x, const ConversationKv& y) {
    return x.format == y.format && x.cells == y.cells && x.heads == y.heads && x.head_dim == y.head_dim &&
           x.page_size == y.page_size && x.pooled_rows == y.pooled_rows && x.idx_dim == y.idx_dim &&
           x.k == y.k && x.v == y.v && x.k_scale == y.k_scale && x.v_scale == y.v_scale && x.pooled == y.pooled;
}

bool same(const SavedConversation& a, const SavedConversation& b) {
    if (a.geometry != b.geometry || a.layer_lo != b.layer_lo || a.layer_hi != b.layer_hi || a.cvec != b.cvec ||
        !same_checkpoint(a.live, b.live) || a.checkpoints.size() != b.checkpoints.size() || a.kv.size() != b.kv.size() ||
        a.stage_images.size() != b.stage_images.size())
        return false;
    for (size_t i = 0; i < a.checkpoints.size(); ++i)
        if (!same_checkpoint(a.checkpoints[i], b.checkpoints[i])) return false;
    for (size_t i = 0; i < a.kv.size(); ++i)
        if (!same_kv(a.kv[i], b.kv[i])) return false;
    for (size_t i = 0; i < a.stage_images.size(); ++i)
        if (!same(a.stage_images[i], b.stage_images[i])) return false;
    return true;
}

// Every field of a v2 entry's index, block table included (the keys carry token ids and images only).  The id is not
// in the index: it is the file's name, and the start-up scan renumbers the entries it takes in.
bool same_entry(const ConversationDiskEntry& a, const ConversationDiskEntry& b) {
    auto same_key = [](const ConversationCheckpoint& x, const ConversationCheckpoint& y) {
        return x.ids == y.ids && x.imgs == y.imgs;
    };
    if (a.cvec != b.cvec || a.bytes != b.bytes || a.geometry != b.geometry ||
        a.tokens != b.tokens || a.written_s != b.written_s || a.last_used_s != b.last_used_s ||
        !same_key(a.live, b.live) || a.checkpoints.size() != b.checkpoints.size() ||
        a.stages.size() != b.stages.size())
        return false;
    for (size_t i = 0; i < a.checkpoints.size(); ++i)
        if (!same_key(a.checkpoints[i], b.checkpoints[i])) return false;
    for (size_t s = 0; s < a.stages.size(); ++s) {
        const auto& x = a.stages[s];
        const auto& y = b.stages[s];
        if (x.file != y.file || x.blocks.size() != y.blocks.size() || x.layer_lo != y.layer_lo ||
            x.layer_hi != y.layer_hi || x.layers != y.layers) return false;
        for (size_t k = 0; k < x.blocks.size(); ++k) {
            const auto& p = x.blocks[k];
            const auto& q = y.blocks[k];
            if (p.kind != q.kind || p.offset != q.offset || p.size != q.size || p.hash_lo != q.hash_lo ||
                p.hash_hi != q.hash_hi || p.tokens_start != q.tokens_start || p.tokens_end != q.tokens_end ||
                p.prefix_digest != q.prefix_digest || p.used != q.used || p.pinned != q.pinned) return false;
        }
    }
    return true;
}

std::vector<int32_t> prompt_of(size_t tokens, int32_t first = 1000) {
    std::vector<int32_t> p;
    for (size_t i = 0; i < tokens; ++i) p.push_back(first + int32_t(i));
    return p;
}

size_t tier_files(const fs::path& dir) {
    size_t n = 0;
    for (const auto& e : fs::directory_iterator(dir))
        if (e.path().filename().string().find(ConversationDiskTier::file_prefix()) != std::string::npos) ++n;
    return n;
}

void touch(const fs::path& p) { std::ofstream(p, std::ios::binary) << "x"; }

// A v1 index file as the previous build wrote it ("STRCIDX1"): the scan must remove it and the files it names.
void write_v1_index(const fs::path& path, uint64_t model, uint64_t config, const std::vector<std::string>& files) {
    std::string b;
    auto put = [&](const void* p, size_t n) { b.append(static_cast<const char*>(p), n); };
    auto u64 = [&](uint64_t v) { put(&v, 8); };
    auto i64 = [&](int64_t v) { put(&v, 8); };
    u64(0x3158444943525453ull);   // "STRCIDX1"
    u64(model);
    u64(config);
    i64(0);                       // written_s
    b.push_back(0);               // cvec
    u64(0);                       // bytes
    const uint32_t n = (uint32_t) files.size();
    put(&n, 4);
    for (const auto& f : files) {
        const uint32_t len = (uint32_t) f.size();
        put(&len, 4);
        b += f;
    }
    const uint32_t none = 0;      // the live key: no tokens, no images; no checkpoints
    put(&none, 4);
    put(&none, 4);
    put(&none, 4);
    std::ofstream f(path, std::ios::binary | std::ios::trunc);
    f.write(b.data(), (std::streamsize) b.size());
}

std::vector<std::string> lines;   // the tier's diagnostic lines
std::mutex lines_m;
ConversationDiskOptions opts(const fs::path& dir, uint64_t budget) {
    ConversationDiskOptions o;
    o.dir = dir.string();
    o.budget_bytes = budget;
    o.log = [](const std::string& l) { std::lock_guard<std::mutex> g(lines_m); lines.push_back(l); };
    return o;
}
bool logged(const char* what) {
    std::lock_guard<std::mutex> g(lines_m);
    for (const auto& l : lines) if (l.find(what) != std::string::npos) return true;
    return false;
}
}  // namespace

int main() {
    const fs::path dir = fs::temp_directory_path() / ("strata_conversation_disk_test_" + std::to_string(std::rand()));
    fs::remove_all(dir);
    const uint64_t GiB = 1ull << 30;
    std::string e;

    {   // off without a folder or a budget
        ConversationDiskTier off(opts("", GiB));
        check(off.open(e) && !off.enabled() && !off.put(image(10, 1)), "no folder: off, not an error");
        ConversationDiskTier off2(opts(dir, 0));
        check(off2.open(e) && !off2.enabled(), "no budget: off");
    }

    {   // container v2: the .bin header and the index's writer/reader (phase 1 step 2; the write path is step 3)
        const fs::path cdir = dir / "container";
        fs::create_directories(cdir);
        uint8_t h[64];
        const ConversationDiskBinHeader head{11, 22, 33, 2};
        conversation_disk_bin_header_write(h, head);
        ConversationDiskBinHeader hback;
        check(conversation_disk_bin_header_read(h, sizeof h, hback), "bin header: reads back");
        check(hback.model == 11 && hback.config == 22 && hback.id == 33 && hback.stage == 2,
              "bin header: every field");
        check(!conversation_disk_bin_header_read(h, 63, hback), "bin header: a short buffer is refused");
        uint8_t wrong[64];
        std::memcpy(wrong, h, sizeof h);
        wrong[0] = 'X';
        check(!conversation_disk_bin_header_read(wrong, sizeof wrong, hback), "bin header: another magic is refused");
        std::memcpy(wrong, h, sizeof h);
        wrong[8] = 1;   // version 3
        check(!conversation_disk_bin_header_read(wrong, sizeof wrong, hback), "bin header: another version is refused");

        auto key_of = [](size_t tokens, uint8_t seed) {
            ConversationCheckpoint c;
            for (size_t i = 0; i < tokens; ++i) c.ids.push_back(1000 + int32_t(i));
            c.imgs.push_back({int64_t(tokens) / 2, 0x1234ull + seed});
            return c;
        };
        ConversationDiskEntry entry;
        entry.id = 7;
        entry.written_s = 1000;
        entry.last_used_s = 2000;
        entry.cvec = false;
        entry.bytes = 4096;
        for (size_t i = 0; i < entry.geometry.size(); ++i) entry.geometry[i] = int64_t(100 + i);
        entry.tokens = 256;
        entry.live = key_of(256, 0);
        entry.checkpoints.push_back(key_of(128, 1));
        entry.checkpoints.push_back(key_of(64, 2));
        for (uint32_t stage = 0; stage < 2; ++stage) {
            ConversationDiskStage s;
            s.file = "strata-conv-0123-7-" + std::to_string(stage) + ".bin";
            s.layer_lo = int64_t(stage) * 24;
            s.layer_hi = s.layer_lo + 24;
            s.layers = 2;
            uint64_t at = kConversationDiskBinHeader;
            for (uint32_t b = 0; b < 2; ++b) {   // a kv block, then the state block at 256
                ConversationDiskBlock block;
                block.kind = b == 0 ? ConversationDiskBlock::kv : ConversationDiskBlock::state;
                block.offset = at;
                block.size = 1024 + 8ull * b;
                block.hash_lo = 0x1111ull + b;
                block.hash_hi = 0x2222ull + b;
                block.tokens_start = b == 0 ? 0 : 256;
                block.tokens_end = 256;
                block.prefix_digest = 0x3333ull + b;
                block.used = b == 1 ? 77 : 0;
                block.pinned = b == 1;
                at += block.size;
                s.blocks.push_back(block);
            }
            entry.stages.push_back(std::move(s));
        }
        const SessionFileIdentity ident{11, 22};
        const fs::path ipath = cdir / "strata-conv-0123-7.idx";
        std::string ierr;
        check(conversation_disk_index_write(ipath.string(), entry, ident, ierr), "index v2: written");
        ConversationDiskEntry back_entry;
        check(conversation_disk_index_read(ipath.string(), ident, back_entry), "index v2: read back");
        check(same_entry(entry, back_entry), "index v2: every field, block record and key comes back");
        check(back_entry.index == ipath.string(), "index v2: the reader names its file");
        ConversationDiskEntry other;
        check(!conversation_disk_index_read(ipath.string(), SessionFileIdentity{11, 23}, other),
              "index v2: another identity is refused");
        const auto full = fs::file_size(ipath);
        fs::resize_file(ipath, full - 8);
        check(!conversation_disk_index_read(ipath.string(), ident, other), "index v2: a truncated index is refused");

        const fs::path bpath = cdir / "broken.idx";
        auto refused = [&](const ConversationDiskEntry& bad, const char* label) {
            other = ConversationDiskEntry{};
            check(conversation_disk_index_write(bpath.string(), bad, ident, ierr) &&
                  !conversation_disk_index_read(bpath.string(), ident, other), label);
        };
        ConversationDiskEntry bad = entry;
        bad.stages[0].blocks[0].kind = 3;
        refused(bad, "index v2: an unknown block kind is refused");
        bad = entry;
        bad.stages[0].blocks[1].tokens_end = 128;   // a state block at one position only
        refused(bad, "index v2: a state block with a range is refused");
        bad = entry;
        bad.stages[0].blocks[1].offset = 100;        // before the header, inside the block before it
        refused(bad, "index v2: an out-of-order block is refused");
        bad = entry;
        bad.stages[0].blocks[0].size = 0;
        refused(bad, "index v2: an empty block is refused");
        bad = entry;
        bad.tokens = 128;                            // not the deepest state block's position
        refused(bad, "index v2: tokens must be the deepest state block's position");
        bad = entry;
        bad.stages.clear();
        refused(bad, "index v2: an entry without a stage is refused");
    }

    {   // start: this tier's leftovers (and their temporary files) go, other files stay
        fs::create_directories(dir);
        touch(dir / (std::string(ConversationDiskTier::file_prefix()) + "0123-1-0.bin"));
        touch(dir / ("." + std::string(ConversationDiskTier::file_prefix()) + "0123-1-0.bin.abcd.tmp"));
        touch(dir / "keep.bin");
        ConversationDiskTier tier(opts(dir, GiB));
        check(tier.open(e) && tier.enabled(), "open");
        check(tier_files(dir) == 0 && logged("2 stale files removed"), "open removes the tier's old and temporary files");
        check(fs::exists(dir / "keep.bin"), "open keeps other files");
        fs::remove(dir / "keep.bin");
    }

    {   // background write, match, read back exactly (a layer split: two .bin and one index; every checkpoint)
        ConversationDiskTier tier(opts(dir, GiB));
        check(tier.open(e), "open 2");
        const SavedConversation expect = split_image(600, 1);
        check(tier.put(split_image(600, 1)), "put a split image");
        tier.wait_idle();
        check(tier.size() == 1 && tier_files(dir) == 3 && !tier.pending(), "written: one .bin per stage, one index");
        check(logged("wrote 600 tokens") && logged("2 files"), "the write is logged");
        const auto live = tier.best(prompt_of(700), {}, false);
        check(live.tokens == 600 && live.live && live.id != 0, "best: the live state as a prefix");
        check(!live.queued && live.bytes == tier.bytes() && live.bytes > 0, "best: a written entry, its files' bytes");
        const auto ck = tier.best([] { auto p = prompt_of(300); p.push_back(7); return p; }(), {}, false);
        check(ck.tokens == 300 && !ck.live, "best: the deepest checkpoint as a prefix");
        check(tier.best([] { auto p = prompt_of(150); p.push_back(7); return p; }(), {}, false).tokens == 150,
              "best: an earlier checkpoint is kept too");
        const auto whole = tier.best(prompt_of(600), {}, false);
        check(whole.tokens == 300 && !whole.live, "best: the whole prompt held is no prefix (its last token is read)");
        check(tier.best(prompt_of(700), {}, true).tokens == 0, "best: another control-vector state does not match");
        check(tier.best(prompt_of(700, 5), {}, false).tokens == 0, "best: other tokens do not match");
        SavedConversation back;
        check(tier.take(live.id, live.tokens, back, e), "take");
        check(same(back, expect), "read back exactly: both stages, the live state and every checkpoint");
        check(tier.size() == 1 && tier.bytes() > 0 && tier_files(dir) == 3,
              "take keeps the entry and its files (step 4: reads no longer delete)");
        SavedConversation twice;
        check(tier.take(live.id, live.tokens, twice, e) && same(twice, expect), "a second read is the same image");
        check(tier.size() == 1 && tier_files(dir) == 3, "the second read keeps them too");
        const auto st = tier.stats();
        check(st.queued == 1 && st.written == 1 && st.disk_hits == 2 && st.ram_hits == 0 && st.fallbacks == 0 &&
              st.tokens_back == 1200 && st.bytes_read == 2 * st.bytes_written && st.bytes_written > (8u << 20),
              "counters");
        check(logged("disk hit: 600 tokens") && logged("totals:"), "the hit and the totals are logged");
    }

    {   // a queued image comes back from RAM: the writer is held at its first write
        std::promise<void> release;
        std::shared_future<void> gate = release.get_future().share();
        auto o = opts(dir, GiB);
        o.before_write = [gate] { gate.wait(); };
        ConversationDiskTier tier(std::move(o));
        check(tier.open(e), "open 3");
        check(tier.put(image(400, 2)), "put a (the writer takes it and waits)");
        check(tier.put(image(400, 3, 5000)), "put b (queued behind a)");
        check(tier.pending(), "pending while the writer waits");
        const auto b = tier.best(prompt_of(450, 5000), {}, false);
        check(b.tokens == 400 && b.queued && b.bytes > 0, "b matches in the queue");
        SavedConversation back;
        check(tier.take(b.id, b.tokens, back, e) && same(back, image(400, 3, 5000)), "b back from RAM");
        check(tier.stats().ram_hits == 1 && logged("RAM hit: 400 tokens"), "a RAM hit, logged");
        release.set_value();
        tier.wait_idle();
        check(!tier.pending() && tier.size() == 1 && tier_files(dir) == 2, "a written after the release");
        check(tier.best(prompt_of(450, 5000), {}, false).tokens == 0, "b is gone from the tier");
    }

    {   // #879: clear() takes the conversations on the disk, their files too
        ConversationDiskTier tier(opts(dir, GiB));
        check(tier.open(e) && tier.put(image(400, 40)), "clear: put a");
        tier.wait_idle();
        check(tier.size() == 1 && tier_files(dir) == 2, "clear: a on the disk");
        check(tier.clear() == 1 && tier.size() == 0 && tier_files(dir) == 0 && logged("removed all 1 conversations"),
              "clear: a and its file gone");
        check(tier.best(prompt_of(450), {}, false).tokens == 0, "clear: nothing matches");
    }

    {   // the budget: the oldest goes for a new one; an image above the budget is refused
        ConversationDiskTier tier(opts(dir, 10ull << 20));   // one ~7 MiB image, not two
        check(tier.open(e), "open 4");
        check(tier.put(image(400, 4)), "first queued");
        tier.wait_idle();
        check(tier.size() == 1, "first fits");
        check(tier.put(image(500, 5, 5000)), "second queued");
        tier.wait_idle();
        check(tier.size() == 1 && tier.stats().evicted == 1, "second evicts the first");
        check(tier.best(prompt_of(450), {}, false).tokens == 0 && tier.best(prompt_of(600, 5000), {}, false).tokens == 500,
              "the newer one stays");
        ConversationDiskTier small(opts(dir, 1ull << 20));
        check(small.open(e) && !small.put(image(400, 6)) && small.size() == 0, "above the budget: refused");
    }
    check(tier_files(dir) == 0, "the tier removes its files when it ends");

    {   // a read the RAM preflight refuses: nothing restored, the files are gone anyway
        ConversationDiskTier tier(opts(dir, GiB));
        check(tier.open(e) && tier.put(split_image(400, 10)), "put for a refused read");
        tier.wait_idle();
        const auto m = tier.best(prompt_of(450), {}, false);
        SavedConversation back;
        const bool ok = tier.take(m.id, m.tokens, back, e, [](uint64_t, std::string& why) { why = "no RAM"; return false; });
        check(!ok && back.kv.empty() && back.stage_images.empty() && tier.size() == 1 && tier_files(dir) == 3,
              "refused read: empty image, the entry and its files stay");
        check(tier.stats().read_failed == 1, "a failed read is counted");
    }

    {   // the RAM cache's evictions go to the disk tier (set_on_evict)
        ConversationDiskTier tier(opts(dir, GiB));
        check(tier.open(e), "open 6");
        ConversationCache cache(GiB, 1);
        cache.set_on_evict([&](SavedConversation&& s) { tier.put(std::move(s)); });
        check(cache.put(image(400, 11)), "park a");
        check(cache.put(image(400, 12, 8000)), "park b: a leaves RAM");
        tier.wait_idle();
        check(cache.size() == 1 && cache.evictions() == 1 && tier.size() == 1, "a is on the disk");
        check(tier.best(prompt_of(450), {}, false).tokens == 400, "a matches on the disk");
        check(cache.best(prompt_of(450, 8000), {}, false).tokens == 400, "b stays in RAM");
    }

    {   // the block store's write path: append (across runs), page-aligned kv blocks, a torn tail cut back
        const fs::path sdir = dir / "store";
        fs::create_directories(sdir);
        const SessionFileIdentity sid{9, 9};
        auto store = [&] {
            auto o = opts(sdir, GiB);
            o.persist = true;
            o.identity = sid;
            return o;
        };
        auto only_index = [&](ConversationDiskEntry& idx) {
            for (const auto& f : fs::directory_iterator(sdir))
                if (f.path().extension() == ".idx") return conversation_disk_index_read(f.path().string(), sid, idx);
            return false;
        };
        auto bin_size = [&] {
            for (const auto& f : fs::directory_iterator(sdir))
                if (f.path().extension() == ".bin") return uint64_t(fs::file_size(f.path()));
            return uint64_t(0);
        };
        {
            ConversationDiskTier tier(store());
            check(tier.open(e) && tier.put(image(600, 30, 1000, 0, 48, 1)), "store: put 600 tokens");
            tier.wait_idle();
        }
        ConversationDiskEntry first;
        check(only_index(first) && first.stages.size() == 1 && first.tokens == 600, "store: one stage, 600 tokens");
        check(first.stages[0].layers == 2 && first.stages[0].layer_lo == 0 && first.stages[0].layer_hi == 48,
              "store: the stage's carve is in the index");
        uint64_t block_bytes = 0;
        for (const auto& b : first.stages[0].blocks) block_bytes += b.size;
        check(first.stages[0].blocks.size() == 4 && first.stages[0].blocks.front().kind == ConversationDiskBlock::kv &&
              first.stages[0].blocks.front().tokens_start == 0 && first.stages[0].blocks.front().tokens_end == 600,
              "store: the first block is the K/V of [0,600), then the three states");
        check(bin_size() == kConversationDiskBinHeader + block_bytes, "store: the .bin is its header and its blocks");
        for (const auto& f : fs::directory_iterator(sdir))
            if (f.path().extension() == ".bin") {   // a torn tail: bytes a crash left past the last indexed block
                std::ofstream out(f.path(), std::ios::binary | std::ios::app);
                out << std::string(1000, 'x');
            }
        const uint64_t torn = bin_size();
        check(torn == kConversationDiskBinHeader + block_bytes + 1000, "store: the tail is there before the append");
        {
            ConversationDiskTier tier(store());
            check(tier.open(e) && tier.size() == 1, "store: the entry is taken in with its tail");
            check(tier.put(image(4200, 30, 1000, 0, 48, 1)), "store: put the longer image");
            tier.wait_idle();
            check(tier.size() == 1 && logged(" (appended)"), "store: it appended to the entry of the earlier run");
        }
        ConversationDiskEntry second;
        check(only_index(second) && second.tokens == 4200 && second.live.ids.size() == 4200,
              "store: the index names the longer state");
        uint64_t total = 0, kv_bytes = 0;
        int64_t kv_at = 0, kv_blocks = 0;
        bool aligned = true, tiled = true;
        size_t states = 0;
        for (const auto& b : second.stages[0].blocks) {
            total += b.size;
            if (b.kind == ConversationDiskBlock::state) {
                ++states;
                continue;
            }
            ++kv_blocks;
            kv_bytes += b.size;
            if (b.tokens_start != kv_at || b.tokens_end - b.tokens_start > 2048) tiled = false;
            if (b.tokens_start % 4 || b.tokens_end % 4) aligned = false;
            kv_at = b.tokens_end;
        }
        check(kv_at == 4200 && tiled && aligned && kv_blocks == 3,
              "store: the kv blocks tile [0,4200) in page-aligned pieces of at most 2048 tokens");
        check(states == 6 && second.stages[0].blocks.size() == 9,
              "store: six states (three per image), appended and not rewritten");
        check(bin_size() == kConversationDiskBinHeader + total,
              "store: the .bin is its header and its blocks, and the torn tail was cut back");
        check(kv_bytes < total && total - block_bytes < kv_bytes,
              "store: the append wrote only the new blocks, not the whole image again");
        {
            ConversationDiskTier tier(store());
            check(tier.open(e) && tier.size() == 1, "store: open to read back");
            const auto m = tier.best(prompt_of(4300), {}, false);
            check(m.tokens == 4200 && m.restore_tokens == 4200, "store: best claims the deepest state");
            SavedConversation back;
            check(tier.take(m.id, m.tokens, back, e), "store: read the appended entry");
            const SavedConversation expect = image(4200, 30, 1000, 0, 48, 1);
            check(same_checkpoint(back.live, expect.live) && back.kv.size() == expect.kv.size() &&
                  same_kv(back.kv[0], expect.kv[0]) && same_kv(back.kv[1], expect.kv[1]),
                  "store: the K/V of both writes came back byte for byte");
            check(back.checkpoints.size() == 5 && same_checkpoint(back.checkpoints[0], expect.checkpoints[0]) &&
                  same_checkpoint(back.checkpoints[1], expect.checkpoints[1]),
                  "store: the two images' checkpoints are there, deepest first");
            check(tier.size() == 1 && tier_files(sdir) == 2,
                  "store: the read keeps the entry and its files (step 4)");
        }

        // a stale shorter copy of the same conversation: the write appends to the longest and removes the shorter one
        ConversationDiskEntry stale;
        std::string ierr;
        fs::remove_all(sdir);   // the read above kept its entry; this part starts from an empty folder
        fs::create_directories(sdir);
        {
            ConversationDiskTier tier(store());
            check(tier.open(e) && tier.put(image(600, 41, 1000, 0, 48, 1)), "supersede: put 600 tokens");
            tier.wait_idle();
        }
        check(only_index(stale) && stale.tokens == 600, "supersede: the 600-token index");
        const fs::path stale_bin = sdir / stale.stages[0].file;   // the index holds the name
        {
            ConversationDiskTier tier(store());
            check(tier.open(e) && tier.put(image(1200, 41, 1000, 0, 48, 1)), "supersede: put 1200 tokens");
            tier.wait_idle();
            check(tier.size() == 1 && logged(" (appended)"), "supersede: appended");
        }
        stale.stages[0].file = (sdir / "strata-conv-0000-stale-0.bin").string();
        fs::copy_file(stale_bin, stale.stages[0].file);
        check(conversation_disk_index_write((sdir / "strata-conv-0000-stale.idx").string(), stale, sid, ierr),
              "supersede: a copy of the shorter index and its .bin, under their own names");
        {
            ConversationDiskTier tier(store());
            check(tier.open(e) && tier.size() == 2, "supersede: both entries taken in");
            check(tier.put(image(1600, 41, 1000, 0, 48, 1)), "supersede: put 1600 tokens");
            tier.wait_idle();
            check(tier.size() == 1 && logged("1 superseded"),
                  "supersede: the write removed the shorter copy of the same conversation");
            check(!fs::exists(stale.stages[0].file) && !fs::exists(sdir / "strata-conv-0000-stale.idx"),
                  "supersede: its .bin and index are gone");
            check(tier.best(prompt_of(1700), {}, false).tokens == 1600, "supersede: the longer entry is the one left");
        }
        fs::remove_all(sdir);
    }

    {   // a folder of the previous build: its v1 conversation is removed at start, a v2 entry beside it stays
        const fs::path vdir = dir / "v1";
        fs::create_directories(vdir);
        const SessionFileIdentity vid{5, 5};
        auto store_v = [&] {
            auto o = opts(vdir, GiB);
            o.persist = true;
            o.identity = vid;
            return o;
        };
        {
            ConversationDiskTier tier(store_v());
            check(tier.open(e) && tier.put(image(300, 50, 1000, 0, 48, 1)), "v1 folder: a v2 entry first");
            tier.wait_idle();
        }
        const fs::path old_bin = vdir / "strata-conv-0123-7-0.bin";
        {
            std::ofstream f(old_bin, std::ios::binary);
            f << "the previous build's session file";
        }
        write_v1_index(vdir / "strata-conv-0123-7.idx", vid.model, vid.config, {old_bin.filename().string()});
        {
            ConversationDiskTier tier(store_v());
            check(tier.open(e), "v1 folder: open");
            check(tier.size() == 1 && tier.best(prompt_of(350), {}, false).tokens == 300,
                  "v1 folder: the v2 entry is taken in");
            check(!fs::exists(old_bin) && !fs::exists(vdir / "strata-conv-0123-7.idx"),
                  "v1 folder: the v1 conversation and its file are removed");
            check(logged("1 refused"), "v1 folder: the start counts it as refused");
            check(tier_files(vdir) == 2, "v1 folder: only the v2 entry's .bin and index are left");
        }
        fs::remove_all(vdir);
    }

    {   // persist: the files outlive the tier, and the next tier with the same identity takes them in
        auto persistent = [&](uint64_t model) {
            auto o = opts(dir, GiB);
            o.persist = true;
            o.identity = {model, 7};
            return o;
        };
        const std::string prefix = ConversationDiskTier::file_prefix();
        {
            ConversationDiskTier tier(persistent(1));
            check(tier.open(e) && tier.persistent(), "persist: open");
            check(tier.put(split_image(500, 20)) && tier.put(image(400, 21, 6000)), "persist: two queued");
        }   // the end waits for the queue and keeps the files
        check(tier_files(dir) == 5 && logged("2 conversations kept on the disk"), "persist: 3 images + 2 indexes kept");
        touch(dir / ("." + prefix + "x-1-0.bin.abcd.tmp"));
        touch(dir / (prefix + "orphan-9-0.bin"));
        {
            ConversationDiskTier tier(persistent(1));
            check(tier.open(e) && tier.size() == 2 && tier_files(dir) == 5, "persist: taken in, leftovers removed");
            check(logged("2 conversations from earlier runs"), "persist: the start is logged");
            const auto m = tier.best(prompt_of(600), {}, false);
            check(m.tokens == 500 && m.live, "persist: matches after a restart");
            SavedConversation back;
            const bool read = tier.take(m.id, m.tokens, back, e);
            if (!read) std::fprintf(stderr, "take: %s\n", e.c_str());
            check(read, "persist: read back after a restart");
            check(same(back, split_image(500, 20)), "persist: the image is exactly the one written");
            check(tier_files(dir) == 5 && tier.size() == 2, "persist: the read keeps its files (step 4)");
        }
        {
            ConversationDiskTier other(persistent(2));   // another model, engine version or settings
            check(other.open(e) && other.size() == 0 && tier_files(dir) == 0 && logged("1 refused"),
                  "persist: another identity's files are refused and removed");
        }
        {
            ConversationDiskTier tier(persistent(3));
            check(tier.open(e) && tier.put(image(300, 22)), "persist: put for a damaged index");
        }
        for (const auto& f : fs::directory_iterator(dir))
            if (f.path().extension() == ".idx") fs::resize_file(f.path(), 20);
        {
            ConversationDiskTier tier(persistent(3));
            check(tier.open(e) && tier.size() == 0 && tier_files(dir) == 0, "persist: a damaged index is refused");
        }
        {
            ConversationDiskTier tier(persistent(4));
            check(tier.open(e) && tier.put(image(400, 23)) && tier.put(image(400, 24, 6000)),
                  "persist: put a and b, the same length");
        }
        std::vector<fs::path> bins;   // a's image first: the names differ only in the id
        for (const auto& f : fs::directory_iterator(dir))
            if (f.path().extension() == ".bin") bins.push_back(f.path());
        std::sort(bins.begin(), bins.end());
        check(bins.size() == 2, "persist: two images");
        ConversationDiskBinHeader b_head;
        {
            uint8_t bytes[kConversationDiskBinHeader];
            std::ifstream f(bins[1], std::ios::binary);
            f.read(reinterpret_cast<char*>(bytes), sizeof bytes);
            check(f.good() && conversation_disk_bin_header_read(bytes, sizeof bytes, b_head), "persist: b's header");
        }
        fs::copy_file(bins[0], bins[1], fs::copy_options::overwrite_existing);   // b's index now names a's image
        {
            // under b's own header (a header of another entry is refused at start, step 5): the blocks' hashes are
            // what must catch a's bytes in b's file
            uint8_t bytes[kConversationDiskBinHeader];
            conversation_disk_bin_header_write(bytes, b_head);
            std::fstream f(bins[1], std::ios::binary | std::ios::in | std::ios::out);
            f.write(reinterpret_cast<const char*>(bytes), sizeof bytes);
        }
        {
            ConversationDiskTier tier(persistent(4));
            check(tier.open(e) && tier.size() == 2, "persist: both taken in");
            const auto m = tier.best(prompt_of(450, 6000), {}, false);
            check(m.tokens == 400, "persist: b matches by its index");
            SavedConversation back;
            check(!tier.take(m.id, m.tokens, back, e) && back.kv.empty() && logged("fails its hash"),
                  "persist: a's image under b's index is refused, never restored as b");
            check(tier.best(prompt_of(450), {}, false).tokens == 400, "persist: a is still there");
        }
    }

    {   // the writer waits for the park's background copy on its own thread, before it reads an image (PARK_ASYNC)
        std::atomic<int> joined{0}, before{0}, saw_image{0};
        auto o = opts(dir, GiB);
        o.before_write = [&] { ++before; };
        o.join = [&](const SavedConversation& img) {
            ++joined;
            if (img.live.ids.size() == 400) ++saw_image;   // the image itself, not one of its stage parts
        };
        ConversationDiskTier tier(std::move(o));
        check(tier.open(e) && tier.put(image(400, 70)), "join: put");
        tier.wait_idle();
        check(joined == 1 && before == 1 && saw_image == 1,
              "join: called once per image, on the writer thread, before the write");
        check(tier.size() == 1 && logged("wrote 400 tokens"), "join: and the write went on");
    }

    {   // the read path: only the blocks the state needs, and a damaged block costs the states above it
        const fs::path rdir = dir / "read";
        fs::create_directories(rdir);
        const SessionFileIdentity rid{4, 4};
        auto store_r = [&] {
            auto o = opts(rdir, GiB);
            o.persist = true;
            o.identity = rid;
            return o;
        };
        {
            ConversationDiskTier tier(store_r());
            check(tier.open(e) && tier.put(image(600, 60, 1000, 0, 48, 1)), "read path: put 600 tokens");
            tier.wait_idle();
            check(tier.put(image(4200, 60, 1000, 0, 48, 1)), "read path: put 4200 tokens (an append)");
            tier.wait_idle();
        }
        ConversationDiskEntry idx;
        for (const auto& f : fs::directory_iterator(rdir))
            if (f.path().extension() == ".idx") conversation_disk_index_read(f.path().string(), rid, idx);
        check(idx.tokens == 4200 && idx.stages.size() == 1, "read path: one entry of 4200 tokens");
        const fs::path rbin = rdir / fs::path(idx.stages[0].file).filename();
        {
            ConversationDiskTier tier(store_r());
            check(tier.open(e) && tier.size() == 1, "read path: open");
            const auto m = tier.best(prompt_of(4300), {}, false);
            check(m.tokens == 4200 && m.restore_tokens == 4200, "read path: the deepest state is claimed");
            SavedConversation back;
            check(tier.take(m.id, m.tokens, back, e), "read path: read");
            const SavedConversation expect = image(4200, 60, 1000, 0, 48, 1);
            check(back.live.ids.size() == 4200 && same_checkpoint(back.live, expect.live) &&
                  back.kv.size() == 2 && same_kv(back.kv[0], expect.kv[0]) && same_kv(back.kv[1], expect.kv[1]),
                  "read path: the K/V of both writes, byte for byte");
            check(back.checkpoints.size() == 5 && back.checkpoints[0].ids.size() == 2100 &&
                  back.checkpoints[4].ids.size() == 150,
                  "read path: the six states, deepest first");
            uint64_t all = 0;
            for (const auto& b : idx.stages[0].blocks) all += b.size;
            const auto st = tier.stats();
            check(tier.size() == 1 && tier_files(rdir) == 2 && st.fallbacks == 0,
                  "read path: the entry and its files stay, nothing fell back");
            check(st.bytes_read == all + kConversationDiskBinHeader,
                  "read path: the deepest state's read takes every block");
            const auto shorter = tier.best(prompt_of(700), {}, false);
            check(shorter.tokens == 600 && !shorter.live, "read path: an earlier state matches a shorter prompt");
            SavedConversation part;
            const uint64_t was = st.bytes_read;
            check(tier.take(shorter.id, shorter.tokens, part, e), "read path: read the earlier state");
            const uint64_t took = tier.stats().bytes_read - was;
            check(took > 0 && took < all, "read path: an earlier state reads only the blocks below it");
            check(part.live.ids.size() == 600 && part.checkpoints.size() == 2,
                  "read path: its own states come back, not the deeper ones");
        }
        // one damaged byte in the middle kv block: the states at 1050 and above need it, 600 and below do not
        uint64_t broken = 0;
        for (const auto& b : idx.stages[0].blocks)
            if (b.kind == ConversationDiskBlock::kv && b.tokens_start == 600) broken = b.offset + b.size / 2;
        check(broken > 0, "damage: the middle kv block is in the index");
        {
            std::fstream f(rbin, std::ios::binary | std::ios::in | std::ios::out);
            char c = 0;
            f.seekg((std::streamoff) broken);
            f.read(&c, 1);
            c = char(c ^ 0x5a);
            f.seekp((std::streamoff) broken);
            f.write(&c, 1);
        }
        {
            ConversationDiskTier tier(store_r());
            check(tier.open(e) && tier.size() == 1, "damage: the entry is still taken in");
            const auto m = tier.best(prompt_of(4300), {}, false);
            check(m.tokens == 4200 && m.restore_tokens == 4200, "damage: the index still claims 4200 before a read");
            SavedConversation back;
            check(tier.take(m.id, m.tokens, back, e), "damage: the read falls back instead of failing");
            check(back.live.ids.size() == 600, "damage: it lands on the state at 600, the deepest that verifies");
            check(back.checkpoints.size() == 2 && back.checkpoints[0].ids.size() == 300 &&
                  back.checkpoints[1].ids.size() == 150, "damage: the states below it come back, deepest first");
            const SavedConversation expect = image(600, 60, 1000, 0, 48, 1);
            check(same_checkpoint(back.live, expect.live) && same_kv(back.kv[0], expect.kv[0]),
                  "damage: the fallback's state and K/V are the ones written first");
            check(logged("fails its hash"), "damage: the error line names the block");
            check(logged("from the state at 600"), "damage: the hit line says where it landed");
            const auto after = tier.best(prompt_of(4300), {}, false);
            check(after.tokens == 4200 && after.restore_tokens == 600,
                  "damage: best() lowers restore_tokens to what a read can give");
            check(tier.stats().fallbacks == 1 && tier.stats().disk_hits == 1, "damage: counted once");
            check(tier.size() == 1 && tier_files(rdir) == 2, "damage: the entry and its files stay");
        }
        {
            ConversationDiskTier tier(store_r());   // and a fresh tier falls back the same way, knowing nothing yet
            check(tier.open(e) && tier.size() == 1 &&
                      tier.best(prompt_of(4300), {}, false).restore_tokens == 4200,
                  "damage: a fresh tier's index knows nothing of the damage until it reads");
            const auto m = tier.best(prompt_of(4300), {}, false);
            SavedConversation back;
            check(tier.take(m.id, m.tokens, back, e) && back.live.ids.size() == 600,
                  "damage: the fresh tier falls back too");
            check(tier.best(prompt_of(4300), {}, false).restore_tokens == 600,
                  "damage: and lowers restore_tokens after it");
        }
        fs::remove_all(rdir);
    }

    {   // the budget by use (step 5): three conversations, the oldest is read, a fourth arrives - the second-oldest
        // goes, not the one just read
        const fs::path bdir = dir / "budget";
        fs::remove_all(bdir);
        fs::create_directories(bdir);
        const uint64_t three = 21ull << 20;   // three ~6.8 MiB conversations, not four
        ConversationDiskTier tier(opts(bdir, three));
        check(tier.open(e), "budget by use: open");
        auto pause = [] { std::this_thread::sleep_for(std::chrono::milliseconds(1100)); };   // the LRU order is seconds
        check(tier.put(image(400, 80, 1000)), "budget by use: put a");
        tier.wait_idle();
        pause();
        check(tier.put(image(400, 81, 5000)), "budget by use: put b");
        tier.wait_idle();
        pause();
        check(tier.put(image(400, 82, 9000)), "budget by use: put c");
        tier.wait_idle();
        check(tier.size() == 3 && tier.stats().evicted == 0, "budget by use: three fit");
        pause();
        const auto a = tier.best(prompt_of(450, 1000), {}, false);
        SavedConversation back;
        check(a.tokens == 400 && a.live && tier.take(a.id, a.tokens, back, e), "budget by use: read a, the oldest");
        pause();
        check(tier.put(image(400, 83, 13000)), "budget by use: put d");
        tier.wait_idle();
        check(tier.size() == 3 && tier.stats().evicted == 1, "budget by use: one conversation goes for d");
        check(tier.best(prompt_of(450, 5000), {}, false).tokens == 0, "budget by use: b, the second oldest, went");
        check(tier.best(prompt_of(450, 1000), {}, false).tokens == 400, "budget by use: a, just read, stayed");
        check(tier.best(prompt_of(450, 9000), {}, false).tokens == 400 &&
                  tier.best(prompt_of(450, 13000), {}, false).tokens == 400,
              "budget by use: c and d stayed");
        fs::remove_all(bdir);
    }

    {   // start-up (step 5): an entry is taken in only if it has a state block, its .bin is as long as the table says
        // and its header names this entry and this stage; a longer .bin stays, its excess ignored
        const fs::path sdir = dir / "startup";
        const SessionFileIdentity sid{8, 8};
        auto store = [&] {
            auto o = opts(sdir, GiB);
            o.persist = true;
            o.identity = sid;
            return o;
        };
        auto reset = [&] {
            fs::remove_all(sdir);
            fs::create_directories(sdir);
            ConversationDiskTier tier(store());
            check(tier.open(e) && tier.put(image(600, 90, 1000, 0, 48, 1)), "start-up: write an entry");
            tier.wait_idle();
        };
        auto files = [&](const char* extension) {
            std::vector<fs::path> out;
            for (const auto& f : fs::directory_iterator(sdir))
                if (f.path().extension() == extension) out.push_back(f.path());
            std::sort(out.begin(), out.end());
            return out;
        };
        auto lines_clear = [] { std::lock_guard<std::mutex> g(lines_m); lines.clear(); };
        auto refused_at_start = [&](const char* label) {
            ConversationDiskTier tier(store());
            check(tier.open(e) && tier.size() == 0 && tier_files(sdir) == 0 && logged("1 refused"), label);
        };

        reset();
        {
            const fs::path index = files(".idx").front();
            ConversationDiskEntry entry;
            check(conversation_disk_index_read(index.string(), sid, entry), "start-up: read the index back");
            auto& blocks = entry.stages[0].blocks;
            blocks.erase(std::remove_if(blocks.begin(), blocks.end(),
                                        [](const ConversationDiskBlock& b) {
                                            return b.kind == ConversationDiskBlock::state;
                                        }),
                         blocks.end());
            std::string ierr;
            check(conversation_disk_index_write(index.string(), entry, sid, ierr),
                  "start-up: publish an index of kv blocks only");
        }
        lines_clear();
        refused_at_start("start-up: an entry with no state block is removed");

        fs::remove_all(sdir);
        fs::create_directories(sdir);
        {
            ConversationDiskTier tier(store());
            check(tier.open(e) && tier.put(split_image(600, 91)), "start-up: write a two-stage entry");
            tier.wait_idle();
        }
        {
            const fs::path index = files(".idx").front();
            ConversationDiskEntry entry;
            check(conversation_disk_index_read(index.string(), sid, entry) && entry.stages.size() == 2,
                  "start-up: two stages");
            auto& blocks = entry.stages[1].blocks;
            blocks.erase(std::remove_if(blocks.begin(), blocks.end(),
                                        [](const ConversationDiskBlock& b) {
                                            return b.kind == ConversationDiskBlock::state;
                                        }),
                         blocks.end());
            std::string ierr;
            check(conversation_disk_index_write(index.string(), entry, sid, ierr),
                  "start-up: publish it without the second stage's state blocks");
        }
        lines_clear();
        refused_at_start("start-up: a stage with no state block is removed");

        reset();
        {
            const fs::path bin = files(".bin").front();
            fs::resize_file(bin, fs::file_size(bin) - 100);   // into the last indexed block
        }
        lines_clear();
        refused_at_start("start-up: a .bin shorter than the table says is removed");

        reset();
        {
            const fs::path bin = files(".bin").front();
            ConversationDiskBinHeader head;
            head.model = sid.model;
            head.config = sid.config;
            head.id = 12345;   // the name says another id
            head.stage = 0;
            uint8_t bytes[kConversationDiskBinHeader];
            conversation_disk_bin_header_write(bytes, head);
            std::fstream f(bin, std::ios::binary | std::ios::in | std::ios::out);
            f.write(reinterpret_cast<const char*>(bytes), sizeof bytes);
        }
        lines_clear();
        refused_at_start("start-up: a .bin whose header names another entry is removed");

        reset();
        {
            const fs::path bin = files(".bin").front();
            ConversationDiskBinHeader head;
            head.model = sid.model;
            head.config = sid.config;
            head.id = 1;       // the entry's own id, but the second stage
            head.stage = 1;
            uint8_t bytes[kConversationDiskBinHeader];
            conversation_disk_bin_header_write(bytes, head);
            std::fstream f(bin, std::ios::binary | std::ios::in | std::ios::out);
            f.write(reinterpret_cast<const char*>(bytes), sizeof bytes);
        }
        lines_clear();
        refused_at_start("start-up: a .bin whose header names another stage is removed");

        reset();
        const uint64_t last_block = fs::file_size(files(".bin").front());
        {
            std::ofstream f(files(".bin").front(), std::ios::binary | std::ios::app);
            f << std::string(1000, 'x');   // a torn tail past the last indexed block
        }
        {
            ConversationDiskTier tier(store());
            check(tier.open(e) && tier.size() == 1, "start-up: a .bin longer than the table is taken in");
            check(fs::file_size(files(".bin").front()) == last_block + 1000,
                  "start-up: and the excess is kept until the next append");
            const auto m = tier.best(prompt_of(650), {}, false);
            SavedConversation back;
            check(m.tokens == 600 && tier.take(m.id, m.tokens, back, e), "start-up: the entry reads back");
            check(tier.size() == 1 && tier_files(sdir) == 2, "start-up: the entry and its files stay");
        }
        fs::remove_all(sdir);
    }

    fs::remove_all(dir);
    std::printf("conversation_disk_test: %d checks passed\n", checks);
    return 0;
}
