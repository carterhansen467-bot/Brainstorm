#include "functions.hpp"
#include "minijson.hpp"
#include "search.hpp"
#include <cstring>
#include <vector>

Item BRAINSTORM_PACK = Item::RETRY;
Item BRAINSTORM_TAG = Item::Charm_Tag;
long BRAINSTORM_SOULS = 1;

long filter(Instance inst) {
    if (BRAINSTORM_PACK != Item::RETRY) {
        inst.cache.generatedFirstPack = true; // we don't care about Pack 1
        if (inst.nextPack(1) != BRAINSTORM_PACK) {
            return 0;
        }
    }
    if (BRAINSTORM_TAG != Item::RETRY) {
        if (inst.nextTag(1) != BRAINSTORM_TAG) {
            return 0;
        }
    }
    if (BRAINSTORM_SOULS > 0) {
        for (int i = 1; i <= BRAINSTORM_SOULS; i++) {
            auto tarots = inst.nextArcanaPack(5, 1); // Mega Arcana Pack
            bool found_soul = false;
            for (int t = 0; t < 5; t++) {
                if (tarots[t] == Item::The_Soul) {
                    found_soul = true;
                    break;
                }
            }
            if (!found_soul) {
                return 0;
            }
        }
    }
    return 1;
};

IMMOLATE_API std::string brainstorm_cpp(std::string seed, std::string pack,
std::string tag, double souls) { BRAINSTORM_PACK = stringToItem(pack);
    BRAINSTORM_TAG = stringToItem(tag);
    BRAINSTORM_SOULS = souls;
    Search search(filter, seed, 1, 100000000);
    search.exitOnFind = true;
    return search.search();
}

struct Step {
    std::string op;
    mini_json::Value args;
};

static Item parseItemSafe(const mini_json::Value &v) {
    if (!v.isString()) return Item::RETRY;
    return stringToItem(v.getString());
}

static bool matchesItem(const Item actual, const mini_json::Value &args) {
    const auto &eq = args["equals"];
    if (eq.isString() && actual != parseItemSafe(eq)) return false;
    const auto &inArr = args["in"];
    if (inArr.isArray()) {
        bool ok = false;
        for (const auto &el : inArr.array) {
            if (el.isString() && actual == parseItemSafe(el)) {
                ok = true;
                break;
            }
        }
        if (!ok) return false;
    }
    return true;
}

static bool matchesJoker(const JokerData &jd, const mini_json::Value &match) {
    if (!match.isObject()) return true;
    if (match["joker"].isString() && jd.joker != parseItemSafe(match["joker"])) return false;
    if (match["rarity"].isString() && jd.rarity != parseItemSafe(match["rarity"])) return false;
    if (match["edition"].isString() && jd.edition != parseItemSafe(match["edition"])) return false;
    const auto &stickers = match["stickers"];
    if (stickers.isObject()) {
        if (stickers["eternal"].isBool() && jd.stickers.eternal != stickers["eternal"].getBool()) return false;
        if (stickers["perishable"].isBool() && jd.stickers.perishable != stickers["perishable"].getBool()) return false;
        if (stickers["rental"].isBool() && jd.stickers.rental != stickers["rental"].getBool()) return false;
    }
    return true;
}

static long long getNumber(const mini_json::Value &v, long long def) {
    return v.isNumber() ? static_cast<long long>(v.number) : def;
}

static bool applyStep(const Step &step, Instance &inst) {
    const auto &args = step.args;
    if (step.op == "tag") {
        int idx = static_cast<int>(getNumber(args["index"], 1));
        Item val = inst.nextTag(idx);
        return matchesItem(val, args);
    }
    if (step.op == "pack") {
        int idx = static_cast<int>(getNumber(args["index"], 1));
        Item val = inst.nextPack(idx);
        return matchesItem(val, args);
    }
    if (step.op == "voucher") {
        int idx = static_cast<int>(getNumber(args["index"], 1));
        Item val = inst.nextVoucher(idx);
        if (!matchesItem(val, args)) return false;
        if (args["activate"].getBool(false)) {
            inst.activateVoucher(val);
        }
        return true;
    }
    if (step.op == "boss") {
        int idx = static_cast<int>(getNumber(args["index"], 1));
        Item val = inst.nextBoss(idx);
        return matchesItem(val, args);
    }
    if (step.op == "joker") {
        int draw = static_cast<int>(getNumber(args["draw"], 1));
        int ante = static_cast<int>(getNumber(args["ante"], 1));
        bool stickers = args["has_stickers"].getBool(true);
        std::string source = args["source"].getString("Brainstorm_Joker");
        JokerData jd = inst.nextJoker(source, ante, stickers);
        // Advance draws if draw > 1
        for (int i = 1; i < draw; i++) {
            inst.nextJoker(source, ante, stickers);
        }
        return matchesJoker(jd, args["match"]);
    }
    if (step.op == "joker_window") {
        int limit = static_cast<int>(getNumber(args["limit"], 1));
        int ante = static_cast<int>(getNumber(args["ante"], 1));
        bool stickers = args["has_stickers"].getBool(true);
        std::string source = args["source"].getString("Brainstorm_Joker_Window");
        const auto &match = args["any"]["match"].isObject() ? args["any"]["match"] : args["match"];
        for (int i = 0; i < limit; i++) {
            JokerData jd = inst.nextJoker(source, ante, stickers);
            if (matchesJoker(jd, match)) return true;
        }
        return false;
    }
    if (step.op == "state") {
        std::string field = args["field"].getString("");
        if (field == "id") {
            long long id = inst.seed.getID();
            const auto &eq = args["equals"];
            if (eq.isNumber() && id != static_cast<long long>(eq.number)) return false;
            const auto &range = args["range"];
            if (range.isArray() && range.array.size() >= 2) {
                long long lo = static_cast<long long>(range.array[0].number);
                long long hi = static_cast<long long>(range.array[1].number);
                if (id < lo || id > hi) return false;
            }
        }
        return true;
    }
    if (step.op == "set") {
        const auto &deck = args["deck"];
        const auto &stake = args["stake"];
        if (deck.isString()) inst.setDeck(stringToItem(deck.getString()));
        if (stake.isString() || stake.isNumber()) {
            if (stake.isString()) {
                inst.setStake(stringToItem(stake.getString()));
            } else {
                inst.setStake(static_cast<Item>(static_cast<int>(stake.number)));
            }
        }
        return true;
    }
    // Unknown op -> fail safely
    return false;
}

static void collectSteps(const mini_json::Value &node, std::vector<Step> &out) {
    if (node.isObject() && node["all"].isArray()) {
        for (const auto &child : node["all"].array) {
            collectSteps(child, out);
        }
        return;
    }
    if (node.isObject() && node["op"].isString()) {
        Step s;
        s.op = node["op"].getString();
        s.args = node["args"];
        out.push_back(s);
    }
}

static const char *dupCString(const std::string &str) {
    char *c_result = (char *)malloc(str.length() + 1);
    if (!c_result) return nullptr;
    std::strcpy(c_result, str.c_str());
    return c_result;
}

IMMOLATE_API const char *brainstorm_query(const char *seed,
                                          const char *query_json) {
    std::string seed_str = seed ? seed : "";
    std::string query_str = query_json ? query_json : "";

    mini_json::Value root;
    if (!mini_json::parse(query_str, root) || !root.isObject()) {
        return dupCString("");
    }

    std::vector<Step> steps;
    collectSteps(root["filter"], steps);
    if (steps.empty()) {
        return dupCString("");
    }

    const auto &search = root["search"];
    int threads = static_cast<int>(getNumber(search["threads"], 1));
    long long max_seeds = getNumber(search["max_seeds"], 100000000);
    bool exit_on_find = search["exit_on_find"].getBool(true);

    Search s([steps](Instance inst) {
        for (const auto &step : steps) {
            if (!applyStep(step, inst)) return 0;
        }
        return 1;
    }, seed_str, threads, max_seeds > 0 ? max_seeds : 100000000);
    s.exitOnFind = exit_on_find;
    std::string result = s.search();
    return dupCString(result);
}

extern "C" {
    IMMOLATE_API const char* brainstorm(const char* seed, const char* pack,
const char* tag, double souls) { std::string cpp_seed(seed); std::string
cpp_pack(pack); std::string cpp_tag(tag); std::string result =
brainstorm_cpp(cpp_seed, cpp_pack, cpp_tag, souls);

        char* c_result = (char*)malloc(result.length() + 1);
        strcpy(c_result, result.c_str());

        return c_result;
    }

    IMMOLATE_API void free_result(const char* result) {
        free((void*)result);
    }
}
