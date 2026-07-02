import os
import sys

from wapiibench.openapi_utils import spec_to_ruleset


def test_spec_parsing() -> None:
    print("Testing specs ...\n")

    passing = []
    failing = []

    spec_dir = "openapi/real_world_specs/"
    for spec_name in sorted(os.listdir(spec_dir)):
        spec_path = os.path.join(spec_dir, spec_name)
        if not os.path.isfile(spec_path) or ".resolved." in spec_name:
            continue

        try:
            spec_to_ruleset(spec_path)
            print(f"Successfully converted {spec_name} to ruleset.\n")
            passing.append(spec_name)
        except Exception as e:
            print(f"Failed to convert {spec_name} to ruleset:\n{e}\n")
            failing.append(spec_name)

    print(f"Passing specs: {passing}")
    print(f"Failing specs: {failing}")


if __name__ == '__main__':
    os.chdir(os.path.normpath(os.path.join(os.path.dirname(__file__), os.pardir)))
    sys.path.append(os.getcwd())

    test_spec_parsing()
