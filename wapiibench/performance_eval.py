from __future__ import annotations

import argparse
import json
import logging
import os
import statistics
import sys
import timeit
from pathlib import Path

import torch
from transformers import GenerationConfig

from evaluation import MODELS, APIS, SETUPS, SETTINGS, instantiate_prompt
from logits_processor import OpenApiDecoder
from model_utils import ModelWrapper
from rag.retriever import Retriever, RetrieverOutputFormat

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _build_input_text(task: dict, setup: str, setting: str, spec_file: str, model_name: str) -> tuple[str, str]:
    """Build the full input text for a single task."""

    with open("resources/code_generation_prompt.md", 'r') as file:
        prompt_template = file.read()

    if 'spec' in setting:
        with open(spec_file, 'r') as file:
            spec = file.read()
    elif 'rag' in setting:
        retriever = Retriever(spec_file, persist_directory=os.path.join("vector_dbs", f"eval_db_{task['api']}"))
        spec = retriever.retrieve_spec_for_task(
            task['task'], num_chunks=5, truncation_threshold=8000, output_format=RetrieverOutputFormat.TYPESCRIPT)
    else:
        spec = ""

    return instantiate_prompt(prompt_template, task, setup, setting, spec=spec, model_name=model_name)


def _run_performance_eval():
    """Measure generation runtime for a single task."""

    parser = argparse.ArgumentParser(
        description="Measure the generation speed (tokens per second etc.) for the given experiment configuration. "
                    "Results are stored under data/performance/.")
    parser.add_argument("--model", required=True, type=str, choices=MODELS, help="HuggingFace model name")
    parser.add_argument("--api", required=True, type=str, choices=APIS.keys(), help="API to benchmark")
    parser.add_argument("--setup", required=True, type=str, choices=SETUPS.keys(), help="Setup to benchmark")
    parser.add_argument("--setting", required=True, type=str, choices=SETTINGS, help="Setting to benchmark")
    parser.add_argument("--task_index", required=True, type=int, help="Index of the task in the dataset")
    parser.add_argument("--runs", default=10, type=int, help="Number of timed generation runs (default: 10)")
    args = parser.parse_args()

    model_name = args.model
    api = args.api
    setup = args.setup
    setting = args.setting
    task_index = args.task_index
    runs = args.runs

    use_cd = 'constrained' in setting
    spec_file = f"openapi/real_world_specs/{api}.yaml"

    # Load dataset
    test_data_file = f"data/synthetic/{api}/test_data_final.json"
    with open(test_data_file, 'r') as file:
        test_data = json.load(file)

    assert 0 <= task_index < len(test_data), f"task_index {task_index} out of range [0, {len(test_data)})"
    task = test_data[task_index]
    task['api'] = api

    # Build prompt
    input_text, starter_code = _build_input_text(task, setup, setting, spec_file, model_name)

    # Load model
    model = ModelWrapper(model_name)
    assert model.provider == 'HuggingFace', "Performance measurement is only supported for HuggingFace models"

    generation_config = GenerationConfig(
        max_new_tokens=250, do_sample=False, num_beams=1, top_k=50, top_p=1.0, stop_strings="\n```\n",
        num_return_sequences=1, pad_token_id=model.tokenizer.eos_token_id)

    logits_processor = OpenApiDecoder(generation_config, model.tokenizer, spec_file) if use_cd else None

    device = str(model.model.device)
    use_cuda = torch.cuda.is_available() and 'cuda' in device

    logger.info(f"Task [{task_index}]: {task['task'][:100]} ...")
    logger.info(f"Device: {device}, setting: {setting}")

    # Define setup function for timeit
    def set_up_generation():
        if use_cd:
            logits_processor.reset(completion_prefix=starter_code)

    # Define generation function for timeit
    def run_generation():
        if use_cuda:
            torch.cuda.synchronize()

        with torch.no_grad():
            output = model.run(input_text, generation_config=generation_config, logits_processor=logits_processor)

        if use_cuda:
            torch.cuda.synchronize()

        return output

    # Warm-up (not measured but used to compute token count)
    set_up_generation()
    output = run_generation()

    # model.run() returns full sequences (input + generated), so subtract input tokens
    input_token_count = len(model.tokenizer.encode(input_text, add_special_tokens=False))
    full_token_count = len(model.tokenizer.encode(output[0], add_special_tokens=False))
    token_count = max(full_token_count - input_token_count, 0)

    logger.debug(f"Input tokens: {input_token_count}, full tokens: {full_token_count}, generated tokens: {token_count}")
    assert token_count <= 250, f"Generated token count {token_count} exceeds max_new_tokens (250)"
    assert input_token_count + token_count == full_token_count, "The math ain't mathing"

    # Timed runs using timeit
    timer = timeit.Timer(stmt=run_generation, setup=set_up_generation)
    all_runtimes = timer.repeat(repeat=runs, number=1)

    for i, t in enumerate(all_runtimes):
        logger.info(f"  Run {i + 1}/{runs}: {t:.4f}s")

    mean_runtime = statistics.mean(all_runtimes)
    tokens_per_sec = token_count / mean_runtime if mean_runtime > 0 else 0
    std_runtime = statistics.stdev(all_runtimes) if len(all_runtimes) > 1 else 0.0
    min_runtime = min(all_runtimes) if all_runtimes else 0.0
    max_runtime = max(all_runtimes) if all_runtimes else 0.0
    logger.info(f"Result: {mean_runtime:.4f}s ± {std_runtime:.4f}s")

    # Save results
    results = {
        "model": model_name,
        "api": api,
        "setup": setup,
        "setting": setting,
        "task_index": task_index,
        "runs": runs,
        "mean_runtime": round(mean_runtime, 6),
        "std_runtime": round(std_runtime, 6),
        "min_runtime": round(min_runtime, 6),
        "max_runtime": round(max_runtime, 6),
        "all_runtimes": [round(t, 6) for t in all_runtimes],
        "token_count": token_count,
        "tokens_per_sec": round(tokens_per_sec, 6),
        "device": device,
        "generation_config": {
            "max_new_tokens": generation_config.max_new_tokens,
            "do_sample": generation_config.do_sample,
            "num_beams": generation_config.num_beams,
            "top_k": generation_config.top_k,
            "top_p": generation_config.top_p,
            "stop_strings": generation_config.stop_strings,
            "num_return_sequences": generation_config.num_return_sequences,
        },
    }

    model_short = model_name.split('/', 1)[1] if '/' in model_name else model_name
    output_dir = Path(f"data/performance/{model_short}/{api}/{setup}/{setting}")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{task_index:04d}_runtime.json"

    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2)
    logger.info(f"Results saved to {output_file}")


if __name__ == "__main__":
    os.chdir(os.path.normpath(os.path.join(os.path.dirname(__file__), os.pardir)))
    sys.path.append(os.getcwd())

    _run_performance_eval()
