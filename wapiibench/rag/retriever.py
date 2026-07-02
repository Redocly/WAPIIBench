from __future__ import annotations

import json
import logging
import os
import shutil
from enum import Enum, auto

import torch
import yaml
from langchain_chroma import Chroma
from langchain_core.documents.base import Document
from langchain_huggingface import HuggingFaceEmbeddings
from sentence_transformers import CrossEncoder

from rag.embedding_text_formatter import format_embedding_text_for_operation_with_description, \
    format_embedding_text_for_operation_with_description_and_parameters, \
    format_embedding_text_for_operation_with_description_and_request_body, \
    format_embedding_text_for_operation_with_description_and_response_schema, \
    format_embedding_text_for_operation_with_description_and_response_example
from rag.typescript_spec_converter import convert_operation_to_typescript
from rag.yaml_preprocessor import HTTP_METHODS, preprocess_yaml_openapi_spec

# Adds a dummy impl that is not included in https://repo.radeon.com/rocm/windows/rocm-rel-7.2/torch-2.9.1%2Brocmsdk20260116-cp312-cp312-win_amd64.whl
if not hasattr(torch.distributed, "is_initialized"):
    setattr(torch.distributed, "is_initialized", lambda: False)

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
DEFAULT_RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
# DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
# DEFAULT_RERANKER_MODEL = "jinaai/jina-reranker-v3"
RERANK_MIN_CANDIDATES = 30
IGNORED_KEYS = ("responses", "tags")


class RetrieverOutputFormat(Enum):
    DICT = auto()
    RAW_YAML = auto()
    TYPESCRIPT = auto()


class Retriever:
    vector_store: Chroma

    def __init__(self, spec_file: str, persist_directory: str | None = None,
                 clear_persist_directory: bool = False) -> None:
        """
        Create a retriever for OpenAPI specifications that can be used for retrieval-augmented generation (RAG).
        :param spec_file: Path to the specification
        :param persist_directory: Directory to persist the vector store in
        :param clear_persist_directory: If True, clear the persist directory. This will force a re-creation of the vector store.
        """
        logger.debug(f"Creating retriever with {spec_file=}, {persist_directory=}, {clear_persist_directory=} ...")

        self._reranker_models: dict[str, CrossEncoder] = {}

        if persist_directory is not None:
            os.makedirs(persist_directory, exist_ok=True)
            metadata_file_path = os.path.join(persist_directory, "metadata.json")
        else:
            metadata_file_path = None

        if persist_directory is not None and not clear_persist_directory:
            if os.path.exists(persist_directory) and os.path.exists(metadata_file_path):
                with open(metadata_file_path, "r") as f:
                    metadata = json.load(f)
                persisted_spec_file = metadata.get("spec_file")

                if persisted_spec_file == spec_file:
                    self.vector_store = Chroma(
                        embedding_function=HuggingFaceEmbeddings(model_name=DEFAULT_EMBEDDING_MODEL),
                        persist_directory=persist_directory)
                    return

                logger.error(
                    f"Spec file `{persisted_spec_file}` in persist directory `{persist_directory}` does not match "
                    f"the specified spec file `{spec_file}`; falling back to in-memory (no persist)")
                persist_directory = None

        if clear_persist_directory and persist_directory is not None and os.path.exists(persist_directory):
            shutil.rmtree(persist_directory)
            os.makedirs(persist_directory, exist_ok=True)

        try:
            with open(spec_file, "r", encoding="utf-8") as f:
                spec_dict = yaml.safe_load(f)

            # Resolve all $ref instances and merge allOf schemas in the spec
            spec_dict = preprocess_yaml_openapi_spec(spec_dict)

            resolved_file_path = os.path.join(
                persist_directory if persist_directory is not None else os.path.dirname(spec_file),
                os.path.basename(spec_file).replace(".yaml", ".resolved.yaml"))
            with open(resolved_file_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(spec_dict, f, width=10000)
        except FileNotFoundError:
            logger.error(f"Specification file `{spec_file}` not found")
            raise
        except yaml.YAMLError:
            logger.error(f"YAML parsing failed for specification file `{spec_file}`")
            raise

        documents = []
        paths_section = spec_dict.get("paths", {})

        for path_name, path_item in paths_section.items():
            if not isinstance(path_item, dict):
                continue

            for method, op_obj in path_item.items():
                if method not in HTTP_METHODS or not isinstance(op_obj, dict):
                    continue

                try:
                    content1 = format_embedding_text_for_operation_with_description(
                        path_name, method, op_obj)
                    content2 = format_embedding_text_for_operation_with_description_and_parameters(
                        path_name, method, op_obj)
                    content3 = format_embedding_text_for_operation_with_description_and_request_body(
                        path_name, method, op_obj)
                    content4 = format_embedding_text_for_operation_with_description_and_response_schema(
                        path_name, method, op_obj)
                    content5 = format_embedding_text_for_operation_with_description_and_response_example(
                        path_name, method, op_obj)
                except ValueError as e:
                    raise ValueError(f"Error processing endpoint {path_name}") from e

                for key in IGNORED_KEYS:
                    op_obj.pop(key, None)
                path_method_item_as_yaml = yaml.safe_dump(op_obj, sort_keys=False)

                documents.extend([Document(page_content=content, metadata={
                    "path_name": path_name,
                    "method": method,
                    "path_method_item": path_method_item_as_yaml,
                }) for content in (content1, content2, content3, content4, content5) if content])

        logger.info(f"Created {len(documents)} endpoint documents")

        self.vector_store = Chroma.from_documents(
            documents, HuggingFaceEmbeddings(model_name=DEFAULT_EMBEDDING_MODEL), persist_directory=persist_directory)

        # Write the metadata file
        if persist_directory is not None:
            metadata = {
                "spec_file": spec_file
            }
            with open(metadata_file_path, "w") as f:
                json.dump(metadata, f)

    def retrieve_spec_for_task(self, task: str, num_chunks: int = 5, truncation_threshold: int | None = None,
                               output_format: RetrieverOutputFormat | str = RetrieverOutputFormat.RAW_YAML,
                               use_reranker: bool = False) -> str | dict:
        """
        Retrieve chunks (endpoints) from the specification that might be relevant for the given task.
        :param task: The task description
        :param num_chunks: Number of chunks to retrieve
        :param truncation_threshold: Maximum number of characters in the retrieved string
        :param output_format: Result format (dict, raw YAML, or TypeScript)
        :param use_reranker: If True, rerank an expanded candidate set before taking top `num_chunks`
        :return: A string with structured endpoint information for LLM consumption or a dict for programmatic use
        """
        retrieval_k = max(num_chunks, RERANK_MIN_CANDIDATES) if use_reranker else num_chunks

        # We put each document up to five times into the store, therefore 5*k. Later we deduplicate by metadata and ensure we're back to the original k.
        docs = self.vector_store.similarity_search(task, k=retrieval_k * 5)
        docs = list({doc.metadata["method"] + doc.metadata["path_name"]: doc for doc in docs}.values())[:retrieval_k]

        docs = self._rerank_docs(task, docs)[:num_chunks] if use_reranker else docs[:num_chunks]
        assert len(docs) == num_chunks, f"Expected {num_chunks} chunks, but got {len(docs)}"

        output_format = _normalize_output_format(output_format)

        if output_format == RetrieverOutputFormat.DICT:
            out = {}
            for doc in docs:
                path_name = doc.metadata["path_name"]
                method = doc.metadata["method"]
                path_method_item = doc.metadata["path_method_item"]
                if path_name not in out:
                    out[path_name] = {}
                out[path_name][method] = yaml.safe_load(path_method_item)
            return out

        if output_format == RetrieverOutputFormat.TYPESCRIPT:
            output = ""
            for doc in docs:
                path_name = doc.metadata["path_name"]
                method = doc.metadata["method"]
                operation = yaml.safe_load(doc.metadata["path_method_item"])
                output += convert_operation_to_typescript(path_name, method, operation) + "\n\n"

        else:  # construct raw YAML output
            output = ""
            for doc in docs:
                # Reconstruct each part of the YAML object using the path_method_item, which is already serialized YAML
                path_method_item = doc.metadata["path_method_item"].replace("\n", "\n    ")
                output += f"{doc.metadata['path_name']}:\n  {doc.metadata['method']}:\n    {path_method_item}\n"

        output = output.strip()

        if truncation_threshold is not None and len(output) > truncation_threshold:
            # Simple truncation strategy: Cut the output before the line in which the threshold is exceeded
            logger.info(f"Truncating output from {len(output)} to {truncation_threshold} characters")
            output = output[:output.rindex("\n", 0, truncation_threshold)] + "\n[TRUNCATED]"

        return output

    def _get_reranker(self, model_name: str) -> CrossEncoder:
        model = self._reranker_models.get(model_name)
        if model is None:
            model = CrossEncoder(model_name, trust_remote_code=True, model_kwargs={'dtype': torch.bfloat16})
            if model.model.config.pad_token_id is None:
                model.model.config.pad_token_id = model.tokenizer.pad_token_id
            self._reranker_models[model_name] = model
        return model

    def _rerank_docs(self, query: str, docs: list[Document]) -> list[Document]:
        model = self._get_reranker(DEFAULT_RERANKER_MODEL)
        pairs = [(query, doc.page_content) for doc in docs]
        scores = model.predict(pairs)
        scored_docs = sorted(zip(scores, docs), key=lambda item: float(item[0]), reverse=True)
        return [doc for _, doc in scored_docs]


def _copy_dict_excluding_keys(d: dict, keys_to_exclude: set[str]) -> dict:
    return {key: value for key, value in d.items() if key not in keys_to_exclude}


def _normalize_output_format(output_format: RetrieverOutputFormat | str) -> RetrieverOutputFormat:
    if isinstance(output_format, RetrieverOutputFormat):
        return output_format

    if isinstance(output_format, str):
        normalized = output_format.strip().lower()
        format = next((format for format in RetrieverOutputFormat if format.name.lower() == normalized), None)
        if format is not None:
            return format

    raise ValueError(f"Unsupported output format: {output_format}")
