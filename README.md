# WAPIIBench

A benchmark for web API integration code generation. For more information, check out our AIware 2025
paper [Benchmarking Web API Integration Code Generation](https://arxiv.org/abs/2509.20172). An appendix to the paper and
all evaluation results are provided in our [artifact](https://doi.org/10.5281/zenodo.13758414).

## Project Overview

### Benchmark

* Evaluation pipeline: [wapiibench/evaluation.py](wapiibench/evaluation.py)

* OpenAPI specifications: [openapi/real_world_specs/](openapi/real_world_specs)

* Dataset creation: [wapiibench/dataset_generation.py](wapiibench/dataset_generation.py)

* Full synthetic dataset: [data/synthetic/all/test_data_final.json](data/synthetic/all/test_data_final.json)

* API-specific subsets: `data/synthetic/{api}/test_data_final.json`

* Codes, configs, logs, results: `data/generated/{model}/{api}/{setup}/{setting}/`

* Aggregated results: `data/generated/{model}/all/{setup}/{setting}/results.json`

* Plots and tables: [wapiibench/export_results.py](wapiibench/export_results.py)

Running the evaluation pipeline:

```commandline
python evaluation.py --models MODELS --apis APIS --setups SETUPS --settings SETTINGS
```

Example:

```commandline
python evaluation.py --models 'bigcode/starcoder2-15b' --apis 'asana' 'google_calendar_v3' 'google_sheet_v4' 'slack' --setups 'invocation' 'endpoint' --settings 'unconstrained'
```

(Further options exist; see `python evaluation.py --help`)

* Validation: [wapiibench/validation.py](wapiibench/validation.py)

* Real-world dataset: [validation_data/all/validation_data_final.json](validation_data/all/validation_data_final.json)

* Validation codes, configs, logs, results: `validation_data/generated/{model}/all/{setup}/{setting}/`

Running the validation pipeline:

```commandline
python validation.py --models MODELS --setups SETUPS --settings SETTINGS
```

Example:

```commandline
python validation.py --models 'bigcode/starcoder2-15b' --setups 'invocation' 'endpoint' --settings 'unconstrained'
```

(Further options exist; see `python validation.py --help`)

* Performance evaluation: [wapiibench/performance_eval.py](wapiibench/performance_eval.py)

* Performance evaluation results: `data/performance/{model}/{api}/{setup}/{setting}/`

### Constrained Decoding

* Translation of OpenAPI specs to regex constraints: [wapiibench/openapi_utils.py](wapiibench/openapi_utils.py)

* Generation rules and rulesets: [wapiibench/generation_rules.py](wapiibench/generation_rules.py)

* Constrained decoding implementation: [wapiibench/logits_processor.py](wapiibench/logits_processor.py)

### Retrieval-Augmented Generation

* Retrieval of endpoint documentation from OpenAPI specs: [wapiibench/rag/retriever.py](wapiibench/rag/retriever.py)

* Preprocessing of specs before embedding: [wapiibench/rag/yaml_preprocessor.py](wapiibench/rag/yaml_preprocessor.py)

* Formatting of retrieved
  chunks: [wapiibench/rag/typescript_spec_converter.py](wapiibench/rag/typescript_spec_converter.py)

* Unused cf-idf-based implementation: [wapiibench/rag/retriever_alternative.py](wapiibench/rag/retriever_alternative.py)

## Setup

The minimum Python version is 3.9. We recommend using a virtual environment.

Install/upgrade basic dependencies:

```commandline
pip install --upgrade 'transformers<5.0.0' torch accelerate numpy openapi3-parser pyyaml regex strenum tqdm
```

Additional optional dependencies:

* `openai` for running API-based models
* `langchain langchain-huggingface langchain-chroma chromadb sentence-transformers` for embedding-based RAG
* `scikit-learn sentence-transformers` for cf-idf-based RAG
* `pandas matplotlib` for plotting results
* `argilla` for data curation

<details>
<summary>Special dependencies for certain models</summary>

* `'transformers<4.50.0'` for codet5p-*b, instructcodet5p-16b, codegen-*B-multi, codegen2-*B_P
* `'transformers<4.41.0' flash-attn` for DeepSeek-Coder-V2-Lite-Base, DeepSeek-Coder-V2-Lite-Instruct (to install
  `flash-attn` with `pip`, use the flags `--use-pep517` `--no-build-isolation`; if that doesn't work, try installing it
  from source)

</details>

Dump dependencies to make setup reproducible:

```commandline
pip freeze > requirements.txt
```

Install/update `node` using `nvm`:

```commandline
nvm install --lts
```

Install JS dependencies (requires `node`):

```commandline
npm install axios axios-mock-adapter
```

Upgrade JS dependencies:

```commandline
npm update
```

## Tips

* To execute the generated code, you need `node` (use the `--node` argument to tell the evaluation pipeline which binary
  to use). Since `node` may not be available in the same environment where you are generating the code, you can use the
  `--generate-only` and `--evaluate-only` flags to separate those two steps.

* To save costs when evaluating OpenAI models, the evaluation pipeline uses OpenAI's Batch API by default (you can
  switch it off by passing `openai_batch=False` to `generate()`). When using the Batch API, the pipeline will submit the
  prompts for all tasks at once and then terminate to wait for them to be processed asynchronously. The next time you
  run the pipeline, it will check if the results are already available, and if so, fetch them and continue with the
  evaluation.

* To evaluate new models, add their checkpoint name to `MODELS` in `evaluation.py`. Additionally, check if they require
  any special treatment in `model_utils.py` (specifically in `_load_hf_model()`).

* To add a new dataset, use `validation.py` as a template and adapt the input and output data paths. To add a new task
  to an existing dataset, simply add a new entry to the corresponding JSON file. If a new task targets an API not yet in
  the `APIS` or `EXTRA_APIS` dict, add a new entry to them and copy the API's specification file in YAML format to
  `openapi/real_world_specs/`.
