import re
import threading
from typing import List, Dict

# The optional ML stack (sentence_transformers/torch, sklearn/scipy)
# is imported LAZILY at first use, not at module import. Core ENMA
# startup must not require native ML libraries: on locked-down
# machines (e.g. Windows Smart App Control) the scipy native DLL can
# be blocked, and a module-level import here would kill the whole
# backend for an OPTIONAL capability. When the stack is unavailable,
# semantic retrieval degrades honestly to keyword/phrase scoring and
# create_embeddings raises — nothing fabricates embeddings.

ML_STATE_AVAILABLE = "AVAILABLE"
ML_STATE_UNAVAILABLE = "UNAVAILABLE"


class DocumentRetriever:

    MODEL_NAME = "all-MiniLM-L6-v2"

    _embedding_model = None
    _embedding_lock = threading.Lock()

    # Optional-subsystem state, recorded at first use.
    _ml_state = None
    _ml_unavailable_reason: str | None = None

    def __init__(
        self,
        top_k=5,
        neighbor_chunks=1,
        semantic_weight=0.60,
        tfidf_weight=0.25,
        keyword_weight=0.10,
        phrase_weight=0.05
    ):
        self.top_k = top_k
        self.neighbor_chunks = neighbor_chunks

        self.semantic_weight = semantic_weight
        self.tfidf_weight = tfidf_weight
        self.keyword_weight = keyword_weight
        self.phrase_weight = phrase_weight

        # IMPORTANT:
        # Do NOT load SentenceTransformer here.
        #
        # The model is loaded lazily only when embeddings
        # or semantic retrieval are actually required.

    @classmethod
    def ml_status(cls) -> dict:
        """
        Honest optional-subsystem state: has the ML stack been
        attempted, and is it AVAILABLE or UNAVAILABLE (with the
        import failure reason). No fabrication either way.
        """

        return {
            "state": cls._ml_state,
            "reason": cls._ml_unavailable_reason,
        }

    @classmethod
    def _load_sentence_transformer(cls):

        from sentence_transformers import SentenceTransformer

        return SentenceTransformer

    @classmethod
    def _get_embedding_model(cls):

        if cls._embedding_model is None:

            with cls._embedding_lock:

                if cls._embedding_model is None:

                    try:
                        SentenceTransformer = (
                            cls._load_sentence_transformer()
                        )
                    except Exception as error:
                        cls._ml_state = ML_STATE_UNAVAILABLE
                        cls._ml_unavailable_reason = (
                            f"sentence_transformers unavailable: "
                            f"{type(error).__name__}: {error}"
                        )
                        raise

                    print(
                        f"Loading embedding model: {cls.MODEL_NAME}"
                    )

                    cls._embedding_model = SentenceTransformer(
                        cls.MODEL_NAME
                    )

                    cls._ml_state = ML_STATE_AVAILABLE
                    cls._ml_unavailable_reason = None

                    print(
                        "Embedding model loaded successfully."
                    )

        return cls._embedding_model

    def _tokenize(self, text: str) -> set:

        return set(
            re.findall(
                r"\b[a-zA-Z0-9]+\b",
                text.lower()
            )
        )

    def _keyword_score(
        self,
        query: str,
        chunk_text: str
    ) -> float:

        query_words = self._tokenize(query)
        chunk_words = self._tokenize(chunk_text)

        if not query_words or not chunk_words:
            return 0.0

        matches = query_words.intersection(chunk_words)

        return len(matches) / len(query_words)

    def _phrase_score(
        self,
        query: str,
        chunk_text: str
    ) -> float:

        query_clean = " ".join(
            query.lower().split()
        )

        chunk_clean = " ".join(
            chunk_text.lower().split()
        )

        if not query_clean:
            return 0.0

        if query_clean in chunk_clean:
            return 1.0

        query_words = query_clean.split()

        if len(query_words) < 2:
            return 0.0

        matched_pairs = 0
        total_pairs = len(query_words) - 1

        for index in range(total_pairs):

            phrase = (
                query_words[index]
                + " "
                + query_words[index + 1]
            )

            if phrase in chunk_clean:
                matched_pairs += 1

        return matched_pairs / total_pairs

    def create_embeddings(
        self,
        chunks: List[Dict]
    ) -> List[Dict]:

        if not chunks:
            return []

        embedding_model = self._get_embedding_model()

        texts = [
            chunk["text"]
            for chunk in chunks
        ]

        embeddings = embedding_model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False
        )

        updated_chunks = []

        for index, chunk in enumerate(chunks):

            updated_chunk = {
                **chunk,
                "embedding": embeddings[index].tolist()
            }

            updated_chunks.append(
                updated_chunk
            )

        return updated_chunks

    def _semantic_scores(
        self,
        query: str,
        chunks: List[Dict]
    ) -> List[float]:

        if not chunks:
            return []

        try:

            from sklearn.metrics.pairwise import (
                cosine_similarity,
            )

            embedding_model = self._get_embedding_model()

            query_embedding = embedding_model.encode(
                [query],
                normalize_embeddings=True,
                show_progress_bar=False
            )

            document_embeddings = [
                chunk["embedding"]
                for chunk in chunks
                if "embedding" in chunk
            ]

            if len(document_embeddings) != len(chunks):

                return [
                    0.0
                    for _ in chunks
                ]

            scores = cosine_similarity(
                query_embedding,
                document_embeddings
            )[0]

            return [
                float(score)
                for score in scores
            ]

        except Exception as error:

            cls = type(self)
            if cls._ml_state is None:
                cls._ml_state = ML_STATE_UNAVAILABLE
                cls._ml_unavailable_reason = (
                    f"semantic scoring unavailable: "
                    f"{type(error).__name__}: {error}"
                )

            print(
                f"Semantic retrieval error: {error}"
            )

            return [
                0.0
                for _ in chunks
            ]

    def _get_numeric_chunk_id(
        self,
        chunk_id
    ):

        """
        Safely convert common chunk ID formats into
        a numeric position.

        Supported examples:

        5
        "5"
        "chunk_5"
        "chunk-5"

        Returns None if no numeric position exists.
        """

        if isinstance(chunk_id, int):
            return chunk_id

        if isinstance(chunk_id, float):
            return int(chunk_id)

        if isinstance(chunk_id, str):

            match = re.search(
                r"(\d+)$",
                chunk_id
            )

            if match:
                return int(
                    match.group(1)
                )

        return None

    def _find_neighbor_chunks(
        self,
        selected,
        chunks,
        selected_ids
    ):

        if self.neighbor_chunks <= 0:
            return []

        chunks_by_position = {}

        for chunk in chunks:

            chunk_id = chunk.get(
                "chunk_id"
            )

            position = self._get_numeric_chunk_id(
                chunk_id
            )

            if position is not None:

                chunks_by_position[
                    position
                ] = chunk

        neighbors = []

        for chunk in list(selected):

            chunk_id = chunk.get(
                "chunk_id"
            )

            position = self._get_numeric_chunk_id(
                chunk_id
            )

            if position is None:
                continue

            for offset in range(
                1,
                self.neighbor_chunks + 1
            ):

                for neighbor_position in (
                    position - offset,
                    position + offset
                ):

                    neighbor = chunks_by_position.get(
                        neighbor_position
                    )

                    if neighbor is None:
                        continue

                    neighbor_id = neighbor.get(
                        "chunk_id"
                    )

                    if neighbor_id in selected_ids:
                        continue

                    neighbors.append(
                        {
                            **neighbor,
                            "score": 0.0,
                            "semantic_score": 0.0,
                            "tfidf_score": 0.0,
                            "keyword_score": 0.0,
                            "phrase_score": 0.0,
                            "is_neighbor": True
                        }
                    )

                    selected_ids.add(
                        neighbor_id
                    )

        return neighbors

    def retrieve(
        self,
        query: str,
        chunks: List[Dict]
    ) -> List[Dict]:

        if not query or not chunks:
            return []

        texts = [
            chunk["text"]
            for chunk in chunks
        ]

        # --------------------------------------------------
        # TF-IDF RETRIEVAL
        # --------------------------------------------------
        # sklearn/scipy are optional native ML dependencies;
        # imported lazily. If unavailable, TF-IDF contributes
        # 0.0 and retrieval degrades honestly to keyword +
        # phrase scoring (scores still reported per chunk).

        try:

            from sklearn.feature_extraction.text import (
                TfidfVectorizer,
            )
            from sklearn.metrics.pairwise import (
                cosine_similarity,
            )

            vectorizer = TfidfVectorizer(
                stop_words="english",
                ngram_range=(1, 2),
                sublinear_tf=True
            )

            document_vectors = vectorizer.fit_transform(
                texts
            )

            query_vector = vectorizer.transform(
                [query]
            )

            tfidf_scores = cosine_similarity(
                query_vector,
                document_vectors
            )[0]

        except ValueError:

            tfidf_scores = [
                0.0
                for _ in chunks
            ]

        except ImportError as error:

            if cls._ml_state is None:
                cls._ml_state = ML_STATE_UNAVAILABLE
                cls._ml_unavailable_reason = (
                    f"sklearn unavailable: "
                    f"{type(error).__name__}: {error}"
                )

            tfidf_scores = [
                0.0
                for _ in chunks
            ]

        # --------------------------------------------------
        # SEMANTIC RETRIEVAL
        # --------------------------------------------------

        semantic_scores = self._semantic_scores(
            query,
            chunks
        )

        # --------------------------------------------------
        # HYBRID SCORING
        # --------------------------------------------------

        scored_chunks = []

        for index, chunk in enumerate(
            chunks
        ):

            keyword_score = self._keyword_score(
                query,
                chunk["text"]
            )

            phrase_score = self._phrase_score(
                query,
                chunk["text"]
            )

            tfidf_score = float(
                tfidf_scores[index]
            )

            semantic_score = float(
                semantic_scores[index]
            )

            final_score = (
                (
                    semantic_score
                    * self.semantic_weight
                )
                +
                (
                    tfidf_score
                    * self.tfidf_weight
                )
                +
                (
                    keyword_score
                    * self.keyword_weight
                )
                +
                (
                    phrase_score
                    * self.phrase_weight
                )
            )

            scored_chunks.append(
                {
                    **chunk,
                    "score": final_score,
                    "semantic_score": semantic_score,
                    "tfidf_score": tfidf_score,
                    "keyword_score": keyword_score,
                    "phrase_score": phrase_score,
                    "is_neighbor": False
                }
            )

        # --------------------------------------------------
        # SORT BY RELEVANCE
        # --------------------------------------------------

        scored_chunks.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        relevant_chunks = [
            chunk
            for chunk in scored_chunks
            if chunk["score"] > 0
        ]

        if not relevant_chunks:
            return []

        # --------------------------------------------------
        # TOP K
        # --------------------------------------------------

        selected = relevant_chunks[
            :self.top_k
        ]

        selected_ids = {
            chunk.get("chunk_id")
            for chunk in selected
        }

        # --------------------------------------------------
        # NEIGHBOR CONTEXT
        # --------------------------------------------------

        neighbors = self._find_neighbor_chunks(
            selected,
            chunks,
            selected_ids
        )

        selected.extend(
            neighbors
        )

        # --------------------------------------------------
        # FINAL ORDER
        # --------------------------------------------------

        def sort_key(chunk):

            position = self._get_numeric_chunk_id(
                chunk.get("chunk_id")
            )

            if position is None:
                return float("inf")

            return position

        selected.sort(
            key=sort_key
        )

        return selected