import argparse
import logging
import os
import sys

from evaluation import MODELS, SETUPS, SETTINGS, generate, execute, compare, analyze

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

EXTRA_APIS = {
    'etherscan_v1': "Etherscan v1",
    'frankerfacez_v1': "FrankerFaceZ v1",
    'github_v3': "GitHub v3 REST",
    'google_maps_platform': "Google Maps Platform",
    'instagram': "Instagram",
    'jsonplaceholder': "Typicode's JSONPlaceholder",
    'npm_registry': "npm Registry",
    'slack': "Slack Web",
    'telegram_bot_v5': "Telegram Bot v5",
    'youtube_data_v3': "YouTube Data v3",
    'zephyr_cloud_v2': "Zephyr Cloud v2",
}


def _run_validation() -> None:
    """Entry point for running the validation. Controlled via command line arguments."""

    default_node = os.path.join(
        os.environ.get("NVM_SYMLINK", os.path.expanduser("~/.nvm/versions/node/v24.16.0/bin")), "node")

    parser = argparse.ArgumentParser(
        description="Run the evaluation with the real-world dataset for the given experiment configuration. "
                    "Results are stored under validation_data/generated/.")
    parser.add_argument("--models", default=MODELS, nargs='+', help="the models to evaluate")
    parser.add_argument("--skip-models", default=(), nargs='+', help="the models to skip")
    parser.add_argument("--setups", default=SETUPS.keys(), nargs='+', help="the setups to evaluate")
    parser.add_argument("--skip-setups", default=(), nargs='+', help="the setups to skip")
    parser.add_argument("--settings", default=SETTINGS, nargs='+', help="the settings to evaluate")
    parser.add_argument("--skip-settings", default=(), nargs='+', help="the settings to skip")
    parser.add_argument("--node", default=default_node, type=str, help="the node binary to use")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--generate-only", action="store_true", help="only generate the code, then stop")
    group.add_argument(
        "--evaluate-only", action="store_true", help="skip code generation, just execute, compare, and analyze it")
    args = parser.parse_args()

    models = [model for model in args.models if model not in args.skip_models]
    setups = [setup for setup in args.setups if setup not in args.skip_setups]
    settings = [setting for setting in args.settings if setting not in args.skip_settings]
    node = args.node
    generate_only = args.generate_only
    evaluate_only = args.evaluate_only

    assert set(models).issubset(MODELS), f"Unsupported model(s): {set(models).difference(MODELS)}"
    assert set(setups).issubset(SETUPS), f"Unsupported setup(s): {set(setups).difference(SETUPS)}"
    assert set(settings).issubset(SETTINGS), f"Unsupported setting(s): {set(settings).difference(SETTINGS)}"

    prompt_file = os.path.join("resources", "code_generation_with_context_prompt.md")
    validation_data_file = os.path.join("validation_data", "all", "validation_data_final.json")

    for model in models:

        data_root = os.path.join("validation_data", "generated", model.split("/", 1)[1])

        for setup in setups:

            for setting in settings:

                logger.info(f"Running evaluation pipeline with {model = }, api = 'all', {setup = }, {setting = } ...")
                output_dir = os.path.join(data_root, 'all', setup, setting)

                if not evaluate_only:
                    generate(model, setup, setting, prompt_file, validation_data_file, output_dir)

                if not generate_only:
                    execute(output_dir, node, test_data_file=validation_data_file)
                    compare(validation_data_file, output_dir)
                    analyze(output_dir)


if __name__ == "__main__":
    os.chdir(os.path.normpath(os.path.join(os.path.dirname(__file__), os.pardir)))
    sys.path.append(os.getcwd())

    _run_validation()
