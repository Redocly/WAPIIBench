import os
import sys

from wapiibench.generation_rules import GenerationRuleset
from wapiibench.openapi_utils import spec_to_ruleset


def test_regex_matching() -> None:
    spec_file = "openapi/test/petstore-expanded-modified.yaml"

    print("Testing METHOD_AS_FUNCTION syntax")
    test_ruleset = spec_to_ruleset(spec_file)
    test_case_dir = "data/test/method_as_function"
    run_regex_tests(test_case_dir, test_ruleset)


def run_regex_tests(test_case_dir: str, test_ruleset: GenerationRuleset):
    positive_test_case_dir = f"{test_case_dir}/positive/"
    negative_test_case_dir = f"{test_case_dir}/negative/"

    passes = 0
    fails = 0
    print("Running test cases ...\n")

    for file_name in os.listdir(positive_test_case_dir):
        file_path = os.path.join(positive_test_case_dir, file_name)
        if not os.path.isfile(file_path) or os.path.splitext(file_name)[1] != ".js":
            print(f"Skipping: {file_name}")
            continue
        with open(file_path, 'r') as file:
            test_case = file.read()
            match = test_ruleset.match_whole_code(test_case, excluded=["funnel"])
            if match:
                passes += 1
            else:
                fails += 1
                first_line = test_case.split("\n")[0].removeprefix("// ")
                print(f"False negative: {file_name} | {first_line}")

    for file_name in os.listdir(negative_test_case_dir):
        file_path = os.path.join(negative_test_case_dir, file_name)
        if not os.path.isfile(file_path) or os.path.splitext(file_name)[1] != ".js":
            print(f"Skipping: {file_name}")
            continue
        with open(file_path, 'r') as file:
            test_case = file.read()
            match = test_ruleset.match_whole_code(test_case, excluded=["funnel"])
            if match:
                fails += 1
                first_line = test_case.split("\n")[0].removeprefix("// ")
                print(f"False positive: {file_name} | {first_line}")
            else:
                passes += 1
    print(f"Total test cases:\t{passes + fails}\nPasses:\t\t\t\t{passes}\nFails:\t\t\t\t{fails}\n")


if __name__ == '__main__':
    os.chdir(os.path.normpath(os.path.join(os.path.dirname(__file__), os.pardir)))
    sys.path.append(os.getcwd())

    test_regex_matching()
