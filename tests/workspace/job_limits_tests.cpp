#define CROW_USE_BOOST 1
#include <boost/asio.hpp>
namespace asio = boost::asio;
#include "crow_all.h"
#include "orderbook_jobs.hpp"
#include <iostream>

namespace {
using namespace dts::orderbook_jobs;
void check(bool value, const char* label) { if (!value) throw std::runtime_error(label); }
template<class F> void rejects(F fn) {
    bool failed = false;
    try { fn(); } catch (const std::exception&) { failed = true; }
    check(failed, "Expected bounded request rejection");
}
Json request(const std::string& mode) {
    auto raw = crow::json::load(R"JSON({"schema_version":1,"mode":"inspect","session_ids":["1"],"source":"mock","configuration":{"quantity":100,"levels":10,"step_seconds":1,"horizon_seconds":30,"lookback_seconds":30,"max_side_age_seconds":5,"target":"buy_cost_bps"},"split":null})JSON");
    Json out(raw); out["mode"] = mode; return out;
}
void validate_json(const Json& value) { validate(crow::json::load(value.dump())); }
Json flow_request() {
    auto out = request("flow");
    out["configuration"] = Json(crow::json::load(R"JSON({"bin_seconds":1,"start_seconds":0,"end_seconds":null,"clock_policy":"strict_receipt"})JSON"));
    return out;
}
Json dataset_request() {
    auto out = request("dataset");
    out["configuration"] = Json(crow::json::load(R"JSON({"levels":5,"return_seconds":60,"max_side_age_seconds":5})JSON"));
    return out;
}
}

int main() {
    const auto root = fs::temp_directory_path() / ("dts-job-limits-" + std::to_string(::getpid()));
    try {
        private_dir(root, true);
        check(dts::research::sha256("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "Empty SHA256 vector");
        check(dts::research::sha256("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "ABC SHA256 vector");
        dts::research::Sha256 million;
        for (int i = 0; i < 1000; ++i) million.update(std::string(1000, 'a'));
        check(million.finish() == "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0", "Million-byte streaming SHA256 vector");
        for (const auto size : {55, 56, 63, 64, 65, 127, 128, 129}) {
            const std::string payload(static_cast<std::size_t>(size), 'q'); dts::research::Sha256 parts;
            for (const auto c : payload) parts.update(&c, 1);
            check(parts.finish() == dts::research::sha256(payload), "SHA256 chunk/padding parity");
            check(parts.finish() == parts.finish(), "Hash finalization is stable and nonmutating");
        }
        validate_json(dataset_request());
        auto dataset = dataset_request(); dataset["configuration"]["return_seconds"] = 120; validate_json(dataset);
        for (const auto* key : {"levels", "return_seconds", "max_side_age_seconds"}) {
            dataset = dataset_request(); dataset["configuration"][key] = 0;
            rejects([&] { validate_json(dataset); });
            dataset = dataset_request(); dataset["configuration"][key] = true;
            rejects([&] { validate_json(dataset); });
        }
        dataset = dataset_request(); dataset["configuration"]["return_seconds"] = 90; rejects([&] { validate_json(dataset); });
        dataset = dataset_request(); dataset["configuration"]["levels"] = 6; rejects([&] { validate_json(dataset); });
        dataset = dataset_request(); dataset["configuration"]["max_side_age_seconds"] = 61.; rejects([&] { validate_json(dataset); });
        dataset = dataset_request(); dataset["configuration"]["clock_override"] = true; rejects([&] { validate_json(dataset); });
        dataset = dataset_request(); dataset["split"] = Json::object(); rejects([&] { validate_json(dataset); });
        const auto dataset_limits = input_limits("dataset");
        check(dataset_limits.session_events == 10000000 && dataset_limits.total_events == 20000000 &&
              dataset_limits.file_bytes == 4000000000ULL && dataset_limits.total_bytes == 8000000000ULL, "Bounded streaming dataset limits");
        rejects([&] { require_space(root, std::numeric_limits<std::uint64_t>::max()); });
        for (const auto* mode : {"inspect", "describe"}) validate_json(request(mode));
        rejects([] { validate_json(request("unknown")); });
        rejects([] { validate_json(request("compare")); });
        validate_json(flow_request());
        auto flow = flow_request(); flow["configuration"]["clock_policy"] = "recorded_monotonic";
        flow["configuration"]["bin_seconds"] = 0.1; flow["configuration"]["end_seconds"] = 60.;
        validate_json(flow);
        for (const auto* key : {"bin_seconds", "start_seconds", "end_seconds"}) {
            auto bad = flow_request(); bad["configuration"][key] = -1.;
            rejects([&] { validate_json(bad); });
            bad = flow_request(); bad["configuration"][key] = true;
            rejects([&] { validate_json(bad); });
        }
        flow = flow_request(); flow["configuration"]["clock_policy"] = "override";
        rejects([&] { validate_json(flow); });
        flow = flow_request(); flow["configuration"]["end_seconds"] = 0.;
        rejects([&] { validate_json(flow); });
        flow = flow_request(); flow["configuration"]["bin_seconds"] = 301.;
        rejects([&] { validate_json(flow); });
        flow = flow_request(); flow["split"] = Json::object();
        rejects([&] { validate_json(flow); });
        flow = flow_request(); flow["configuration"]["clock_override"] = true;
        rejects([&] { validate_json(flow); });
        auto description = request("describe"); description["split"] = Json::object();
        rejects([&] { validate_json(description); });
        description = request("describe"); description["configuration"]["levels"] = 11;
        rejects([&] { validate_json(description); });
        description = request("describe"); description["configuration"]["clock_override"] = true;
        rejects([&] { validate_json(description); });
        description = request("describe"); description["source"] = "external";
        rejects([&] { validate_json(description); });
        const auto larger = input_limits("describe");
        check(larger.session_events == 500000 && larger.total_events == 500000,
              "Description has bounded per-session and combined events");
        check(larger.file_bytes == 200000000 && larger.total_bytes == 200000000,
              "Description has bounded file and combined bytes");
        const auto timed_description = input_limits("flow");
        check(timed_description.session_events == 500000 && timed_description.total_events == 500000 &&
              timed_description.file_bytes == 200000000 && timed_description.total_bytes == 200000000,
              "Explicit flow exploration stays bounded");
        for (const auto* mode : {"inspect", "compare", "variation"}) {
            const auto original = input_limits(mode);
            check(original.session_events == 200000 && original.total_events == 300000,
                  "Timed/model event limits remain unchanged");
            check(original.file_bytes == 80000000 && original.total_bytes == 120000000,
                  "Timed/model byte limits remain unchanged");
        }
        // Exercise the actual writer at its boundary without writing a large file.
        const auto path = root / "bounded.json";
        {
            SnapshotWriter writer(path, 10);
            writer.append("123456"); writer.append("7890");
            rejects([&] { writer.append("1"); });
            check(writer.finish() == 10, "Rejected bytes cannot alter saved length");
        }
        check(read_file(path, 10) == "1234567890", "Bounded input bytes are exact");
        rejects([&] { SnapshotWriter collision(path, 10); });
        check((fs::status(path).permissions() & fs::perms::group_all) == fs::perms::none,
              "Frozen snapshot is private");
        // The writer accepts a dataset stream above the previous 80 MB file
        // bound with small appends and a constant-sized hash state.
        {
            SnapshotWriter writer(root / "large.jsonl", dataset_limits.file_bytes);
            const std::string chunk(65536, 'x'); dts::research::Sha256 expected;
            for (int i = 0; i < 1250; ++i) { writer.append(chunk); expected.update(chunk); }
            check(writer.finish() == 81920000 && writer.sha256() == expected.finish(), "Large bounded incremental snapshot hash");
        }
        const auto output = root / "output"; private_dir(output, true);
        write_file(output / "blocks.csv", "session,value\n1,2\n");
        Json entry; entry["name"] = "blocks"; entry["file"] = "blocks.csv"; entry["content_type"] = "text/csv";
        entry["bytes"] = 18; entry["rows"] = 1; entry["sha256"] = dts::research::sha256("session,value\n1,2\n");
        // Compute the literal length so the test protects exact byte accounting.
        entry["bytes"] = static_cast<std::uint64_t>(std::string("session,value\n1,2\n").size());
        auto verified = verified_artifact(output, crow::json::load(entry.dump()), "blocks");
        { std::ofstream changed(output / "blocks.csv", std::ios::trunc); changed << "tampered"; }
        char saved[64]{}; const auto saved_size = ::read(verified.file.fd, saved, sizeof(saved));
        check(saved_size == 18 && std::string(saved, static_cast<std::size_t>(saved_size)) == "session,value\n1,2\n", "Verified download survives saved-file edits");
        rejects([&] { (void)verified_artifact(output, crow::json::load(entry.dump()), "blocks"); });
        rejects([&] { (void)verified_artifact(output, crow::json::load(entry.dump()), "../blocks.csv"); });
        fs::remove(output / "blocks.csv"); fs::create_symlink(path, output / "blocks.csv");
        rejects([&] { (void)verified_artifact(output, crow::json::load(entry.dump()), "blocks"); });
        fs::remove_all(root);
        std::cout << "Description request and bounded snapshot tests passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
