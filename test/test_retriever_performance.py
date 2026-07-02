import json
import os
import sys

from tqdm import tqdm

from helpers.match_urls import match_urls
from wapiibench.rag.retriever import Retriever, RetrieverOutputFormat

# Paths (assumes running from repo root)
DATA_FILE = "data/test/test_data_cleaned.json"
SPECS_DIR = "openapi/real_world_specs/"
PERSIST_DIR = "vector_dbs/test_db_{}"

# Mapping from api key to spec file
API_TO_SPEC = {
    "asana": "asana.yaml",
    "google_calendar_v3": "google_calendar_v3.yaml",
    "google_sheet_v4": "google_sheet_v4.yaml",
    "slack": "slack.yaml",
}


def test_retriever_performance():
    if not os.path.isfile(DATA_FILE):
        print(f"Creating {DATA_FILE} ...")
        match_urls()
        print()

    # Load test data
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        test_data = json.load(f)

    # Create retrievers for each API
    retrievers = {}
    for api_name, spec_file in API_TO_SPEC.items():
        spec_path = os.path.join(SPECS_DIR, spec_file)
        if os.path.exists(spec_path):
            print(f"Creating retriever for {api_name} ...")
            api_persist_dir = PERSIST_DIR.format(api_name)
            retrievers[api_name] = Retriever(spec_path, persist_directory=api_persist_dir, clear_persist_directory=True)
        else:
            print(f"Warning: Spec file {spec_path} not found for API {api_name}")

    successes = 0
    failures = []

    # Track results per API
    results_per_api = {api: {"successes": 0, "failures": []} for api in API_TO_SPEC.keys()}

    print("Running tests ...", flush=True)
    for item in tqdm(test_data):
        api = item["api"]
        task = item["task"]
        config = item["config"]
        expected_url = config["url_clean"]
        index = item["index"]

        if api not in retrievers:
            raise ValueError(f"No retriever for API: {api}")

        retriever = retrievers[api]
        result = retriever.retrieve_spec_for_task(
            task, num_chunks=5, output_format=RetrieverOutputFormat.DICT, use_reranker=False)

        # Result is a dict where keys are paths like "/attachments" or "/attachments/{attachment_gid}"
        retrieved_urls = list(result.keys())

        if expected_url in retrieved_urls:
            successes += 1
            results_per_api[api]["successes"] += 1
        else:
            failure_info = {
                "api": api,
                "index": index,
                "task": task,
                "expected_url": expected_url,
                "retrieved_urls": retrieved_urls,
                "reason": "Expected URL not in retrieved results"
            }
            failures.append(failure_info)
            results_per_api[api]["failures"].append(failure_info)

    # Print results per API
    for api_name in API_TO_SPEC.keys():
        api_successes = results_per_api[api_name]["successes"]
        api_failures = results_per_api[api_name]["failures"]
        api_total = api_successes + len(api_failures)

        print("\n" + "=" * 60)
        print(f"TEST RESULTS - {api_name}")
        print("=" * 60)
        print(f"Successes: {api_successes}")
        print(f"Failures: {len(api_failures)}")
        print(f"Total: {api_total}")
        if api_total > 0:
            print(f"Success Rate: {api_successes / api_total * 100:.2f}%")

        if api_failures:
            print("\n" + "-" * 60)
            print("FAILURE DETAILS")
            print("-" * 60)
            for failure in api_failures:
                print(f"\nIndex: {failure['index']}")
                print(f"Task: {failure['task']}")
                print(f"Expected URL: {failure['expected_url']}")
                print(f"Retrieved URLs: {failure['retrieved_urls']}")

    # Print overall results
    print("\n" + "=" * 60)
    print("OVERALL TEST RESULTS")
    print("=" * 60)
    print(f"Successes: {successes}")
    print(f"Failures: {len(failures)}")
    print(f"Total: {successes + len(failures)}")
    print(f"Success Rate: {successes / (successes + len(failures)) * 100:.2f}%")


if __name__ == "__main__":
    os.chdir(os.path.normpath(os.path.join(os.path.dirname(__file__), os.pardir)))
    sys.path.append(os.getcwd())

    test_retriever_performance()
