"""
Mock backends for test rig.
Provides in-memory search with both agent-level and query-level documents.
"""
import json
import numpy as np
from typing import List, Dict, Any
from pathlib import Path


class MockVectorSearchBackend:
    """
    Mock search backend that supports both retrieval strategies.

    Stores two types of documents:
    1. Agent documents (for agent strategy)
    2. Query documents (for query strategy)
    """

    def __init__(self, agent_data_path: str = "test_data/sample_agents.json"):
        self.agent_docs = []
        self.query_docs = []
        self.initialized = False
        self.agent_data_path = agent_data_path

    async def initialize(self):
        """Load sample data and create mock embeddings"""
        if self.initialized:
            return

        # Load agent data
        data_path = Path(self.agent_data_path)
        if not data_path.exists():
            print(f"Warning: {data_path} not found, using empty dataset")
            self.initialized = True
            return

        with open(data_path) as f:
            agents = json.load(f)

        print(f"Loading {len(agents)} sample agents...")

        # Create agent-level documents
        for agent in agents:
            agent_doc = {
                "id": f"agent-{agent['id']}",
                "url": agent["url"],
                "name": agent["name"],
                "description": agent.get("description", ""),
                "json_ld": json.dumps({
                    "@type": agent["type"],
                    "name": agent["name"],
                    "url": agent["url"],
                    "version": "1.0.0",
                    "capabilities": agent.get("capabilities", ""),
                    "skills": agent.get("skills", []),
                    "tools": agent.get("tools", [])
                }),
                "site": "test",
                # Mock embedding based on name+description
                "embedding": self._create_mock_embedding(
                    agent["name"] + " " + agent.get("description", "") + " " + agent.get("capabilities", "")
                ),
                "@search.score": 1.0
            }
            self.agent_docs.append(agent_doc)

        # Create query-level documents
        for agent in agents:
            if "sample_queries" in agent:
                for idx, query in enumerate(agent["sample_queries"]):
                    query_doc = {
                        "id": f"query-{agent['id']}-{idx}",
                        "url": f"{agent['url']}/query-{idx}",
                        "name": query["short"],
                        "agent_id": agent["id"],
                        "agent_name": agent["name"],
                        "agent_url": agent["url"],
                        "query": query["short"],
                        "query_detail": query["detail"],
                        "agent_json_ld": json.dumps({
                            "@type": agent["type"],
                            "name": agent["name"],
                            "url": agent["url"],
                            "version": "1.0.0",
                            "skills": agent.get("skills", []),
                            "tools": agent.get("tools", [])
                        }),
                        "description": query["detail"],
                        # Mock embedding based on query text
                        "embedding": self._create_mock_embedding(
                            query["short"] + " " + query["detail"]
                        ),
                        "@search.score": 1.0
                    }
                    self.query_docs.append(query_doc)

        print(f"Created {len(self.agent_docs)} agent documents")
        print(f"Created {len(self.query_docs)} query documents")

        self.initialized = True

    def _create_mock_embedding(self, text: str) -> List[float]:
        """Create a deterministic mock embedding based on text hash"""
        # Use text hash to create a deterministic but varied embedding
        np.random.seed(hash(text.lower()) % (2**32))
        embedding = np.random.randn(1536).tolist()
        return embedding

    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """Compute cosine similarity between two vectors"""
        vec1_np = np.array(vec1)
        vec2_np = np.array(vec2)
        return float(np.dot(vec1_np, vec2_np) / (np.linalg.norm(vec1_np) * np.linalg.norm(vec2_np)))

    async def search(self, query: str, vector: List[float], top_k: int) -> List[Dict[str, Any]]:
        """
        Search for documents.

        For agent strategy: returns agent documents
        For query strategy: returns query documents (detected by larger top_k)
        """
        if not self.initialized:
            await self.initialize()

        # Heuristic: if top_k > 30, assume query strategy (needs more docs for aggregation)
        use_query_docs = top_k > 30

        documents = self.query_docs if use_query_docs else self.agent_docs

        if not documents:
            return []

        # Compute similarities
        results = []
        for doc in documents:
            similarity = self._cosine_similarity(vector, doc["embedding"])

            result_doc = {**doc}
            result_doc["@search.score"] = similarity
            results.append(result_doc)

        # Sort by similarity and return top_k
        results.sort(key=lambda x: x["@search.score"], reverse=True)

        doc_type = "query" if use_query_docs else "agent"
        print(f"Mock search found {len(results)} {doc_type} documents, returning top {top_k}")

        return results[:top_k]

    async def close(self):
        """Cleanup"""
        pass


class MockLLMBackend:
    """
    Mock LLM backend for testing.
    Provides realistic scoring based on query-agent matching.
    """

    def __init__(self):
        self.embeddings_cache = {}
        # Predefined scoring rules for common queries
        self.scoring_rules = {
            "writing": ["Writing Coach", "Code Reviewer"],
            "translate": ["Writing Coach"],
            "code": ["Code Reviewer"],
            "review": ["Code Reviewer", "Writing Coach"],
            "travel": ["Travel Concierge"],
            "trip": ["Travel Concierge"],
            "hotel": ["Travel Concierge"],
            "flight": ["Travel Concierge"],
            "recipe": ["Recipe Finder"],
            "cook": ["Recipe Finder"],
            "meal": ["Recipe Finder"],
            "weather": ["Weather Service"],
            "forecast": ["Weather Service"],
        }

    async def initialize(self):
        """Initialize LLM backend"""
        pass

    async def get_embedding(self, text: str) -> List[float]:
        """Generate mock embedding for text"""
        if text in self.embeddings_cache:
            return self.embeddings_cache[text]

        # Create deterministic embedding based on text
        np.random.seed(hash(text.lower()) % (2**32))
        embedding = np.random.randn(1536).tolist()
        self.embeddings_cache[text] = embedding

        return embedding

    async def rank_site(self, query: str, site_json: str) -> Dict[str, Any]:
        """
        Rank a site based on query.
        Uses simple keyword matching for realistic scores.
        """
        try:
            site_data = json.loads(site_json) if isinstance(site_json, str) else site_json
        except:
            site_data = {}

        # Extract name from site data
        name = site_data.get("name", "Unknown")
        description = site_data.get("description", "")

        # For aggregated agents (query strategy), look at matched capabilities
        matched_capabilities = site_data.get("matched_capabilities", [])
        capabilities_text = " ".join([
            cap.get("capability", "") + " " + cap.get("description", "")
            for cap in matched_capabilities
        ])

        # Combine all text for matching
        full_text = f"{name} {description} {capabilities_text}".lower()
        query_lower = query.lower()

        # Calculate score based on keyword matching
        score = 50  # Base score

        # Check scoring rules
        for keyword, relevant_agents in self.scoring_rules.items():
            if keyword in query_lower:
                if name in relevant_agents:
                    score += 30
                    break

        # Bonus for direct keyword matches in text
        query_words = query_lower.split()
        matches = sum(1 for word in query_words if word in full_text)
        score += min(matches * 5, 20)

        # Cap at 100
        score = min(score, 100)

        # Generate description
        if score >= 80:
            relevance = "Excellent match"
        elif score >= 70:
            relevance = "Good match"
        elif score >= 60:
            relevance = "Moderate match"
        else:
            relevance = "Weak match"

        description_text = f"{relevance} for query '{query}'"

        return {
            "score": score,
            "description": description_text
        }

    async def close(self):
        """Cleanup"""
        pass
