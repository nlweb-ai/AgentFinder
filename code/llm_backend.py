"""
Swappable LLM/embedding backend interface.
Implement LLMBackend class for your provider (Azure OpenAI, OpenAI, Anthropic, etc.)
"""
import os
import json
import itertools
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

# Configuration from environment variables
LLM_CONFIG = {
    "provider": os.getenv("LLM_PROVIDER", "azure_openai"),  # azure_openai, openai, anthropic
    "endpoint": os.getenv("LLM_ENDPOINT"),  # Must be set via environment variable
    "api_key": os.getenv("LLM_API_KEY"),  # Must be set via environment variable
    "model": os.getenv("LLM_MODEL", "gpt-4"),
    "embedding_model": os.getenv("LLM_EMBEDDING_MODEL", "text-embedding-3-large"),
    "max_concurrent": int(os.getenv("LLM_MAX_CONCURRENT", "1000")),
    "api_version": os.getenv("LLM_API_VERSION", "2024-02-01"),
}


def _rank_prompt(query: str, augment_description: str) -> str:
    """Query-time relevance scoring: how well does this augment answer the query."""
    return f"""Assign a score between 0 and 100 to the following agent based on the likelihood that the agent will contain an answer to the user's question.

First think about the kind of thing the user is seeking and then verify that the agent is primarily focused on that kind of thing.

The user's question is: {query}

The agent's description is:
{augment_description}

Return JSON only with this exact format: {{"score": <integer 0-100>, "description": "<one sentence explanation>"}}"""


def _quality_prompt(text: str) -> str:
    """Curation scoring: judge an augment on its intrinsic quality, not a query."""
    return f"""You are curating a catalog of capabilities (MCP servers, agents, skills) for an AI agent to discover and use. Rate the following entry from 0 to 100 on its overall quality and usefulness.

Score HIGH when the entry: has a clear, specific description of concrete capabilities; exposes broadly useful functionality; and appears to be a real, legitimate, maintained capability.

Score LOW when the entry: has a vague, empty, or generic description; appears to be a test, demo, example, placeholder, or template; looks like spam or a near-duplicate; or describes something with little practical utility.

Judge the entry on its own merits — do NOT reward marketing buzzwords ("powerful", "best", "amazing") that are not backed by specific capabilities.

The entry is:
{text}

Return JSON only with this exact format: {{"score": <integer 0-100>, "description": "<one sentence justification>"}}"""


def _chat_kwargs(model: str, max_tokens: int, temperature: float) -> dict:
    """Build chat-completion kwargs compatible with the target model.

    Newer reasoning models (gpt-5*, o1/o3/o4*) reject `max_tokens` (require
    `max_completion_tokens`) and only accept the default temperature. Detect
    those by name and adjust, so callers can pass the same args for any model.
    """
    m = (model or "").lower()
    reasoning = m.startswith(("gpt-5", "o1", "o3", "o4"))
    kwargs: dict = {}
    if reasoning:
        kwargs["max_completion_tokens"] = max_tokens
        # temperature other than the default is unsupported; omit it.
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["temperature"] = temperature
    return kwargs


class LLMBackend(ABC):
    """Abstract base for LLM backends"""

    @abstractmethod
    async def initialize(self):
        """Initialize clients and connection pools"""
        pass

    @abstractmethod
    async def get_embedding(self, text: str) -> List[float]:
        """Get embedding vector for text"""
        pass

    @abstractmethod
    async def rank_augment(self, query: str, augment_description: str, model: Optional[str] = None) -> Dict[str, Any]:
        """
        Rank an agent for a query.
        Args:
            query: User's natural language query
            augment_description: Agent description to rank
            model: Optional model override (if None, uses configured model)
        Returns: {"score": int, "description": str}
        """
        pass

    @abstractmethod
    async def score_quality(self, text: str, model: Optional[str] = None) -> Dict[str, Any]:
        """
        Score an entry's intrinsic quality/usefulness for catalog curation (no query).
        Returns: {"score": int, "description": str}
        """
        pass

    @abstractmethod
    async def close(self):
        """Cleanup connections"""
        pass


class AzureOpenAIBackend(LLMBackend):
    """Azure OpenAI implementation"""

    def __init__(self):
        self.clients = []
        self.client_cycle = None

    async def initialize(self):
        """Initialize Azure OpenAI clients with connection pooling"""
        from openai import AsyncAzureOpenAI

        # Validate required configuration
        if not LLM_CONFIG["endpoint"]:
            raise ValueError(
                "LLM_ENDPOINT environment variable is required. "
                "Please set it to your Azure OpenAI endpoint URL (e.g., https://your-openai.openai.azure.com)"
            )

        if not LLM_CONFIG["api_key"]:
            raise ValueError(
                "LLM_API_KEY environment variable is required. "
                "Please set it to your Azure OpenAI API key"
            )

        # Create pool of clients for parallel calls
        num_clients = min(100, LLM_CONFIG["max_concurrent"] // 10)
        for i in range(num_clients):
            client = AsyncAzureOpenAI(
                azure_endpoint=LLM_CONFIG["endpoint"],
                api_key=LLM_CONFIG["api_key"],
                api_version=LLM_CONFIG["api_version"],
                max_retries=1,
                timeout=8.0
            )
            self.clients.append(client)

        # Create round-robin client selector
        self.client_cycle = itertools.cycle(self.clients)

        print(f"Azure OpenAI initialized with {len(self.clients)} clients, max {LLM_CONFIG['max_concurrent']} concurrent calls")

    async def get_embedding(self, text: str) -> List[float]:
        """Get embedding vector from Azure OpenAI"""
        client = next(self.client_cycle)

        try:
            response = await client.embeddings.create(
                model=LLM_CONFIG["embedding_model"],
                input=text[:8000]  # Limit input length
            )
            return response.data[0].embedding
        except Exception as e:
            print(f"Embedding error: {e}")
            # Return zero vector on error (will rank low)
            return [0.0] * 1536  # Default embedding size

    async def _score(self, prompt: str, model: Optional[str] = None) -> Dict[str, Any]:
        """Run a scoring prompt and parse the {score, description} JSON result."""
        from openai import APITimeoutError, APIError

        client = next(self.client_cycle)
        model_to_use = model if model else LLM_CONFIG["model"]

        try:
            response = await client.chat.completions.create(
                model=model_to_use,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=100
            )

            result = json.loads(response.choices[0].message.content)

            if "score" not in result:
                result["score"] = 0
            if "description" not in result:
                result["description"] = "No description provided"
            result["score"] = max(0, min(100, int(result["score"])))
            return result

        except APITimeoutError as e:
            print(f"Scoring timeout (8s exceeded): {str(e)[:100]}")
            return {"score": 0, "description": "Scoring timed out"}
        except (APIError, Exception) as e:
            print(f"Scoring error: {str(e)[:100]}")
            return {"score": 0, "description": "Scoring failed"}

    async def rank_augment(self, query: str, augment_description: str, model: Optional[str] = None) -> Dict[str, Any]:
        """Rank an agent for a query using Azure OpenAI"""
        return await self._score(_rank_prompt(query, augment_description), model)

    async def score_quality(self, text: str, model: Optional[str] = None) -> Dict[str, Any]:
        """Score an entry's intrinsic quality for curation using Azure OpenAI"""
        return await self._score(_quality_prompt(text), model)

    async def generate(self, messages: List[Dict[str, str]], model: Optional[str] = None,
                        max_tokens: int = 1024, temperature: float = 0.0):
        """Free-form chat completion (no forced JSON).

        Returns (text, usage) where usage is
        {"prompt_tokens", "completion_tokens", "total_tokens"}.
        """
        client = next(self.client_cycle)
        resolved = model or LLM_CONFIG["model"]
        response = await client.chat.completions.create(
            model=resolved,
            messages=messages,
            **_chat_kwargs(resolved, max_tokens, temperature),
        )
        u = response.usage
        usage = {
            "prompt_tokens": u.prompt_tokens if u else 0,
            "completion_tokens": u.completion_tokens if u else 0,
            "total_tokens": u.total_tokens if u else 0,
        }
        return response.choices[0].message.content or "", usage

    async def close(self):
        """Cleanup - OpenAI clients don't need explicit cleanup"""
        pass


class OpenAIBackend(LLMBackend):
    """OpenAI (non-Azure) implementation"""

    def __init__(self):
        self.clients = []
        self.client_cycle = None

    async def initialize(self):
        """Initialize OpenAI clients"""
        from openai import AsyncOpenAI

        # Create pool of clients
        num_clients = min(100, LLM_CONFIG["max_concurrent"] // 10)
        for i in range(num_clients):
            client = AsyncOpenAI(
                api_key=LLM_CONFIG["api_key"],
                max_retries=1,
                timeout=10.0
            )
            self.clients.append(client)

        self.client_cycle = itertools.cycle(self.clients)
        print(f"OpenAI initialized with {len(self.clients)} clients")

    async def get_embedding(self, text: str) -> List[float]:
        """Get embedding from OpenAI"""
        client = next(self.client_cycle)

        try:
            response = await client.embeddings.create(
                model=LLM_CONFIG["embedding_model"],
                input=text[:8000]
            )
            return response.data[0].embedding
        except Exception as e:
            print(f"Embedding error: {e}")
            return [0.0] * 1536

    async def _score(self, prompt: str, model: Optional[str] = None) -> Dict[str, Any]:
        """Run a scoring prompt and parse the {score, description} JSON result."""
        client = next(self.client_cycle)
        model_to_use = model if model else LLM_CONFIG["model"]

        try:
            response = await client.chat.completions.create(
                model=model_to_use,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=100
            )

            result = json.loads(response.choices[0].message.content)
            result["score"] = max(0, min(100, int(result.get("score", 0))))
            if "description" not in result:
                result["description"] = "No description"
            return result
        except Exception as e:
            print(f"Scoring error: {str(e)[:100]}")
            return {"score": 0, "description": "Scoring failed"}

    async def rank_augment(self, query: str, augment_description: str, model: Optional[str] = None) -> Dict[str, Any]:
        """Rank an agent for a query using OpenAI"""
        return await self._score(_rank_prompt(query, augment_description), model)

    async def score_quality(self, text: str, model: Optional[str] = None) -> Dict[str, Any]:
        """Score an entry's intrinsic quality for curation using OpenAI"""
        return await self._score(_quality_prompt(text), model)

    async def generate(self, messages: List[Dict[str, str]], model: Optional[str] = None,
                        max_tokens: int = 1024, temperature: float = 0.0):
        """Free-form chat completion (no forced JSON).

        Returns (text, usage) where usage is
        {"prompt_tokens", "completion_tokens", "total_tokens"}.
        """
        client = next(self.client_cycle)
        resolved = model or LLM_CONFIG["model"]
        response = await client.chat.completions.create(
            model=resolved,
            messages=messages,
            **_chat_kwargs(resolved, max_tokens, temperature),
        )
        u = response.usage
        usage = {
            "prompt_tokens": u.prompt_tokens if u else 0,
            "completion_tokens": u.completion_tokens if u else 0,
            "total_tokens": u.total_tokens if u else 0,
        }
        return response.choices[0].message.content or "", usage

    async def close(self):
        """Cleanup"""
        pass


class AnthropicBackend(LLMBackend):
    """Anthropic Claude implementation (placeholder)"""

    def __init__(self):
        self.client = None

    async def initialize(self):
        """Initialize Anthropic client"""
        # Example implementation
        # from anthropic import AsyncAnthropic
        # self.client = AsyncAnthropic(api_key=LLM_CONFIG["api_key"])
        raise NotImplementedError("Anthropic backend not yet implemented")

    async def get_embedding(self, text: str) -> List[float]:
        """Anthropic doesn't provide embeddings - would need a separate service"""
        raise NotImplementedError("Anthropic doesn't provide embeddings - use OpenAI for embeddings")

    async def rank_augment(self, query: str, augment_description: str, model: Optional[str] = None) -> Dict[str, Any]:
        """Rank using Claude"""
        raise NotImplementedError("Anthropic backend not yet implemented")

    async def score_quality(self, text: str, model: Optional[str] = None) -> Dict[str, Any]:
        """Score quality using Claude"""
        raise NotImplementedError("Anthropic backend not yet implemented")

    async def close(self):
        """Cleanup"""
        pass


# Factory function
def get_llm_backend() -> LLMBackend:
    """Get the configured LLM backend"""
    provider = LLM_CONFIG["provider"].lower()

    if provider == "azure_openai":
        return AzureOpenAIBackend()
    elif provider == "openai":
        return OpenAIBackend()
    elif provider == "anthropic":
        return AnthropicBackend()
    else:
        raise ValueError(f"Unknown LLM provider: {LLM_CONFIG['provider']}")