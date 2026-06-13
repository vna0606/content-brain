"""
embedder.py — детерминированные embeddings через хэш + numpy.

Claude API не имеет dedicated embeddings endpoint. Используем
детерминированный хэш-based вектор (hashlib → стабилен между сессиями).
Для продакшн-точности можно заменить на sentence-transformers локально.
"""

import hashlib
import numpy as np

EMBEDDING_DIM = 256


def embed_text(text: str) -> np.ndarray:
    """
    Детерминированный псевдо-эмбеддинг через SHA-256 seed.
    Один и тот же текст всегда даёт один и тот же вектор.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:4], "big")
    rng = np.random.default_rng(seed)
    vec = rng.standard_normal(EMBEDDING_DIM).astype(np.float32)
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Косинусное сходство двух нормализованных векторов."""
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


def vec_to_blob(vec: np.ndarray) -> bytes:
    """numpy float32 → bytes для хранения в Turso BLOB."""
    return vec.astype(np.float32).tobytes()


def blob_to_vec(blob: bytes) -> np.ndarray:
    """bytes BLOB → numpy float32 вектор."""
    return np.frombuffer(blob, dtype=np.float32)
