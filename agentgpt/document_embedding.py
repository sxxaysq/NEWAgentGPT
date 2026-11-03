"""Remote document vectors, separate from the existing long-term memory index."""

import hashlib
import math

import httpx


class DocumentEmbedding:
    def __init__(self, settings):
        from tokenizers import Tokenizer

        self.settings = settings
        if not settings.document_embedding_tokenizer:
            raise ValueError("Remote document embeddings require their exact tokenizer")
        self.tokenizer = Tokenizer.from_file(settings.document_embedding_tokenizer)
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        identity = "|".join(
            str(x)
            for x in (
                settings.document_embedding_url,
                settings.document_embedding_model,
                settings.document_embedding_dimensions,
                settings.document_embedding_max_tokens,
                hashlib.sha256(self.tokenizer.to_str().encode()).hexdigest(),
            )
        )
        self.collection = "documents_remote_" + hashlib.sha256(identity.encode()).hexdigest()[:16]

    def embed(self, texts):
        if any(
            len(self.tokenizer.encode(t).ids) > self.settings.document_embedding_max_tokens
            for t in texts
        ):
            raise ValueError("Document input exceeds remote embedding token limit")
        with httpx.Client(timeout=60, trust_env=False) as client:
            response = client.post(
                self.settings.document_embedding_url,
                json={
                    "model": self.settings.document_embedding_model,
                    "input": texts,
                    "max_length": self.settings.document_embedding_max_tokens,
                },
            )
            response.raise_for_status()
            data = response.json()["data"]
        if len(data) != len(texts) or {x["index"] for x in data} != set(range(len(texts))):
            raise ValueError("Incomplete embedding response")
        vectors = [x["embedding"] for x in sorted(data, key=lambda x: x["index"])]
        for vector in vectors:
            if (
                len(vector) != self.settings.document_embedding_dimensions
                or not all(
                    isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                    for x in vector
                )
                or not any(vector)
            ):
                raise ValueError("Invalid embedding dimensions or values")
        return vectors
