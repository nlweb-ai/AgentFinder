"""
Test suite for WHO handler implementation.
Tests compliance with Who Protocol specification (Version 0.1).
"""
import asyncio
import json
import pytest
import pytest_asyncio
from typing import Dict, Any, List
from unittest.mock import Mock, AsyncMock, patch

# Import the handler
import who_handler


class MockSearchBackend:
    """Mock search backend for testing"""

    def __init__(self, mock_results: List[Dict[str, Any]] = None):
        self.mock_results = mock_results or []

    async def initialize(self):
        pass

    async def search(self, query: str, vector: List[float], top_k: int = 30) -> List[Dict[str, Any]]:
        return self.mock_results

    async def close(self):
        pass


class MockLLMBackend:
    """Mock LLM backend for testing"""

    def __init__(self, mock_embedding: List[float] = None, mock_rankings: Dict[str, Dict[str, Any]] = None):
        self.mock_embedding = mock_embedding or [0.1] * 1536
        self.mock_rankings = mock_rankings or {}
        self.rank_site_calls = []  # Track calls for debugging

    async def initialize(self):
        pass

    async def get_embedding(self, text: str) -> List[float]:
        return self.mock_embedding

    async def rank_site(self, query: str, site_json: str) -> Dict[str, Any]:
        """
        Rank a site based on its JSON-LD.
        In tests, we'll include a 'url' field in the json_ld to match against mock_rankings.
        """
        self.rank_site_calls.append((query, site_json))

        # Parse site_json to get identifier for matching
        try:
            site_data = json.loads(site_json) if site_json else {}

            # Try matching by 'url' field in json_ld
            url = site_data.get("url", "")
            if url and url in self.mock_rankings:
                return self.mock_rankings[url]

            # Try matching by 'name' field
            name = site_data.get("name", "")
            if name and name in self.mock_rankings:
                return self.mock_rankings[name]

            # Try matching by @type
            type_val = site_data.get("@type", "")
            if type_val and type_val in self.mock_rankings:
                return self.mock_rankings[type_val]

        except Exception as e:
            print(f"Mock rank_site error: {e}")

        # Default return - high score so tests pass
        return {"score": 80, "description": "Default mock ranking"}

    async def close(self):
        pass


@pytest_asyncio.fixture
async def handler():
    """Create a WHO handler with mock backends"""
    h = who_handler.WHOHandler()

    # Mock backends
    h.search_backend = MockSearchBackend()
    h.llm_backend = MockLLMBackend()

    await h.search_backend.initialize()
    await h.llm_backend.initialize()

    # Helper method to setup rankings more easily
    def setup_rankings(rankings_dict):
        """
        Setup rankings for test.
        rankings_dict: {url: {"score": int, "description": str}}
        """
        h.llm_backend.mock_rankings = rankings_dict

    h.setup_rankings = setup_rankings

    yield h

    await h.cleanup()


class TestWhoProtocolCompliance:
    """Test compliance with Who Protocol specification"""

    @pytest.mark.asyncio
    async def test_empty_query_returns_error(self, handler):
        """Test that empty query returns INVALID_QUERY error (Section 6.2)"""
        result = await who_handler.who_query("")

        assert result["_meta"]["response_type"] == "failure"
        assert result["_meta"]["version"] == "0.1"
        assert result["error"]["code"] == "INVALID_QUERY"
        assert result["error"]["message"] == "Query text is required"

    @pytest.mark.asyncio
    async def test_no_results_returns_empty_list(self, handler):
        """Test that no results returns empty results array"""
        handler.search_backend.mock_results = []

        result = await handler.process_query("test query")

        assert result["_meta"]["response_type"] == "answer"
        assert result["_meta"]["version"] == "0.1"
        assert result["_meta"]["result_count"] == 0
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_response_structure(self, handler):
        """Test that response follows protocol structure (Section 6.1)"""
        # Setup mock data
        handler.search_backend.mock_results = [
            {
                "url": "https://example.com",
                "name": "Test Site",
                "json_ld": json.dumps({"@type": "A2AAgent"}),
                "site": "test"
            }
        ]
        handler.llm_backend.mock_rankings = {
            "https://example.com": {"score": 85, "description": "Good match"}
        }

        result = await handler.process_query("test query")

        # Check _meta structure
        assert "_meta" in result
        assert result["_meta"]["response_type"] == "answer"
        assert result["_meta"]["version"] == "0.1"
        assert "result_count" in result["_meta"]

        # Check results array
        assert "results" in result
        assert isinstance(result["results"], list)

    @pytest.mark.asyncio
    async def test_result_object_structure(self, handler):
        """Test that result objects have required fields (Section 5.3)"""
        handler.search_backend.mock_results = [
            {
                "url": "https://example.com",
                "name": "Test Agent",
                "json_ld": json.dumps({
                    "@type": "A2AAgent",
                    "name": "Test Agent",
                    "url": "https://example.com",  # Include URL for mock matching
                    "skills": []
                }),
                "site": "test"
            }
        ]
        handler.llm_backend.mock_rankings = {
            "https://example.com": {"score": 90, "description": "Excellent match"}
        }

        result = await handler.process_query("test query")

        assert len(result["results"]) > 0

        # Check required fields
        augment = result["results"][0]
        assert "protocol" in augment
        assert "endpoint" in augment
        assert "score" in augment
        assert "definition" in augment

        # Check score is 0-100
        assert 0 <= augment["score"] <= 100


class TestProtocolTypes:
    """Test different protocol types (Section 5)"""

    @pytest.mark.asyncio
    async def test_a2a_agent_format(self, handler):
        """Test A2A Agent Card format (Section 5.2)"""
        handler.search_backend.mock_results = [
            {
                "url": "https://travel.example.com",
                "name": "Travel Agent",
                "json_ld": json.dumps({
                    "@type": "A2AAgent",
                    "name": "Travel Concierge",
                    "url": "https://travel.example.com",  # Include for mock matching
                    "version": "1.0.0",
                    "capabilities": {"streaming": True},
                    "skills": [
                        {
                            "id": "plan_trip",
                            "name": "Trip Planning",
                            "description": "Plan trips",
                            "examples": ["plan a trip to Japan"]
                        }
                    ]
                }),
                "site": "test"
            }
        ]
        handler.llm_backend.mock_rankings = {
            "https://travel.example.com": {"score": 95, "description": "Travel planning expert"}
        }

        result = await handler.process_query("plan a trip")

        assert len(result["results"]) > 0
        augment = result["results"][0]

        assert augment["protocol"] == "a2a"
        assert augment["endpoint"] == "https://travel.example.com"

        # Check A2A Agent Card structure
        definition = augment["definition"]
        assert definition["name"] == "Travel Agent"
        assert "description" in definition
        assert definition["url"] == "https://travel.example.com"
        assert definition["version"] == "1.0.0"
        assert "capabilities" in definition
        assert "skills" in definition

    @pytest.mark.asyncio
    async def test_mcp_server_format(self, handler):
        """Test MCP server format (Section 5.2)"""
        handler.search_backend.mock_results = [
            {
                "url": "https://weather.example.com",
                "name": "Weather Service",
                "json_ld": json.dumps({
                    "@type": "MCPServer",
                    "name": "Weather Service",
                    "url": "https://weather.example.com",  # Include for mock matching
                    "version": "1.0.0",
                    "tools": [
                        {
                            "name": "get_weather",
                            "description": "Get weather for a location",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "location": {"type": "string"}
                                }
                            }
                        }
                    ]
                }),
                "site": "test"
            }
        ]
        handler.llm_backend.mock_rankings = {
            "https://weather.example.com": {"score": 97, "description": "Weather data"}
        }

        result = await handler.process_query("get weather")

        assert len(result["results"]) > 0
        augment = result["results"][0]

        assert augment["protocol"] == "mcp"

        # Check MCP server structure
        definition = augment["definition"]
        assert definition["name"] == "Weather Service"
        assert "tools" in definition
        assert len(definition["tools"]) > 0
        assert definition["version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_skill_format(self, handler):
        """Test Agent Skill format (Section 5.2)"""
        handler.search_backend.mock_results = [
            {
                "url": "https://skills.example.com/pptx-creator",
                "name": "pptx-creator",
                "json_ld": json.dumps({
                    "@type": "Skill",
                    "name": "pptx-creator",
                    "url": "https://skills.example.com/pptx-creator",  # Include for mock matching
                    "license": "MIT",
                    "compatibility": "python3",
                    "metadata": {
                        "author": "example-org",
                        "version": "1.0.0",
                        "tags": ["presentations"]
                    }
                }),
                "site": "test"
            }
        ]
        handler.llm_backend.mock_rankings = {
            "https://skills.example.com/pptx-creator": {
                "score": 89,
                "description": "Create PowerPoint presentations"
            }
        }

        result = await handler.process_query("create presentation")

        assert len(result["results"]) > 0
        augment = result["results"][0]

        assert augment["protocol"] == "skill"

        # Check skill structure
        definition = augment["definition"]
        assert definition["name"] == "pptx-creator"
        assert definition["license"] == "MIT"
        assert definition["compatibility"] == "python3"
        assert "metadata" in definition

    @pytest.mark.asyncio
    async def test_openapi_format(self, handler):
        """Test OpenAPI service format (Section 5.2)"""
        handler.search_backend.mock_results = [
            {
                "url": "https://api.example.com",
                "name": "Inventory API",
                "json_ld": json.dumps({
                    "@type": "OpenAPIService",
                    "url": "https://api.example.com",  # Include for mock matching
                    "specUrl": "https://api.example.com/openapi.json"
                }),
                "site": "test"
            }
        ]
        handler.llm_backend.mock_rankings = {
            "https://api.example.com": {"score": 80, "description": "Inventory management"}
        }

        result = await handler.process_query("check inventory")

        assert len(result["results"]) > 0
        augment = result["results"][0]

        assert augment["protocol"] == "openapi"

        # Check OpenAPI structure
        definition = augment["definition"]
        assert definition["name"] == "Inventory API"
        assert "specUrl" in definition

    @pytest.mark.asyncio
    async def test_http_format(self, handler):
        """Test custom HTTP endpoint format (Section 5.2)"""
        handler.search_backend.mock_results = [
            {
                "url": "https://legacy.example.com/api/query",
                "name": "Legacy API",
                "json_ld": json.dumps({
                    "@type": "CustomEndpoint",
                    "url": "https://legacy.example.com/api/query",  # Include for mock matching
                    "method": "POST",
                    "contentType": "application/json",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"}
                        }
                    }
                }),
                "site": "test"
            }
        ]
        handler.llm_backend.mock_rankings = {
            "https://legacy.example.com/api/query": {
                "score": 75,
                "description": "Legacy system"
            }
        }

        result = await handler.process_query("query legacy system")

        assert len(result["results"]) > 0
        augment = result["results"][0]

        assert augment["protocol"] == "http"

        # Check HTTP endpoint structure
        definition = augment["definition"]
        assert definition["name"] == "Legacy API"
        assert definition["method"] == "POST"
        assert definition["contentType"] == "application/json"
        assert "inputSchema" in definition


class TestFiltering:
    """Test query filtering (Section 3.1)"""

    @pytest.mark.asyncio
    async def test_type_filter(self, handler):
        """Test filtering by augment type"""
        handler.search_backend.mock_results = [
            {
                "url": "https://agent.example.com",
                "name": "Test Agent",
                "json_ld": json.dumps({"@type": "A2AAgent"}),
                "site": "test"
            },
            {
                "url": "https://tool.example.com",
                "name": "Test Tool",
                "json_ld": json.dumps({"@type": "MCPTool"}),
                "site": "test"
            }
        ]
        handler.llm_backend.mock_rankings = {
            "https://agent.example.com": {"score": 90, "description": "Agent"},
            "https://tool.example.com": {"score": 85, "description": "Tool"}
        }

        # Filter for A2A agents only
        result = await handler.process_query("test query", augment_type="A2AAgent")

        # Should only return A2A agents
        for augment in result["results"]:
            assert augment["protocol"] == "a2a"

    @pytest.mark.asyncio
    async def test_max_results_limit(self, handler):
        """Test max_results parameter"""
        # Create 10 mock results
        handler.search_backend.mock_results = [
            {
                "url": f"https://site{i}.example.com",
                "name": f"Site {i}",
                "json_ld": json.dumps({"@type": "A2AAgent"}),
                "site": "test"
            }
            for i in range(10)
        ]
        handler.llm_backend.mock_rankings = {
            f"https://site{i}.example.com": {"score": 80 + i, "description": f"Site {i}"}
            for i in range(10)
        }

        # Request only 3 results
        result = await handler.process_query("test query", max_results=3)

        assert len(result["results"]) <= 3
        assert result["_meta"]["result_count"] <= 3


class TestErrorHandling:
    """Test error handling (Section 6.2)"""

    @pytest.mark.asyncio
    async def test_invalid_query_error(self):
        """Test INVALID_QUERY error code"""
        result = await who_handler.who_query("")

        assert result["_meta"]["response_type"] == "failure"
        assert result["error"]["code"] == "INVALID_QUERY"

    @pytest.mark.asyncio
    async def test_error_response_structure(self):
        """Test error response structure"""
        result = await who_handler.who_query(None)

        assert "_meta" in result
        assert result["_meta"]["response_type"] == "failure"
        assert result["_meta"]["version"] == "0.1"
        assert "error" in result
        assert "code" in result["error"]
        assert "message" in result["error"]


class TestRanking:
    """Test ranking and scoring"""

    @pytest.mark.asyncio
    async def test_score_threshold(self, handler):
        """Test that low-scoring results are filtered out"""
        handler.search_backend.mock_results = [
            {
                "url": "https://high.example.com",
                "name": "High Score",
                "json_ld": json.dumps({
                    "@type": "A2AAgent",
                    "url": "https://high.example.com"
                }),
                "site": "test"
            },
            {
                "url": "https://low.example.com",
                "name": "Low Score",
                "json_ld": json.dumps({
                    "@type": "A2AAgent",
                    "url": "https://low.example.com"
                }),
                "site": "test"
            }
        ]
        handler.llm_backend.mock_rankings = {
            "https://high.example.com": {"score": 95, "description": "Excellent"},
            "https://low.example.com": {"score": 20, "description": "Poor match"}
        }

        result = await handler.process_query("test query")

        # Low scoring result should be filtered out (default threshold is 70)
        assert len(result["results"]) == 1
        assert result["results"][0]["score"] >= 70

    @pytest.mark.asyncio
    async def test_results_sorted_by_score(self, handler):
        """Test that results are sorted by score descending"""
        handler.search_backend.mock_results = [
            {
                "url": f"https://site{i}.example.com",
                "name": f"Site {i}",
                "json_ld": json.dumps({
                    "@type": "A2AAgent",
                    "url": f"https://site{i}.example.com"
                }),
                "site": "test"
            }
            for i in range(5)
        ]
        # Assign random scores
        handler.llm_backend.mock_rankings = {
            "https://site0.example.com": {"score": 75, "description": "Site 0"},
            "https://site1.example.com": {"score": 95, "description": "Site 1"},
            "https://site2.example.com": {"score": 85, "description": "Site 2"},
            "https://site3.example.com": {"score": 80, "description": "Site 3"},
            "https://site4.example.com": {"score": 90, "description": "Site 4"},
        }

        result = await handler.process_query("test query")

        # Check results are sorted by score
        scores = [r["score"] for r in result["results"]]
        assert scores == sorted(scores, reverse=True)

        # Highest score should be first
        assert result["results"][0]["score"] == 95


class TestCaching:
    """Test caching behavior"""

    @pytest.mark.asyncio
    async def test_embedding_cache(self, handler):
        """Test that embeddings are cached"""
        call_count = 0
        original_get_embedding = handler.llm_backend.get_embedding

        async def counting_get_embedding(text: str):
            nonlocal call_count
            call_count += 1
            return await original_get_embedding(text)

        handler.llm_backend.get_embedding = counting_get_embedding
        handler.search_backend.mock_results = []

        # First query
        await handler.process_query("test query")
        assert call_count == 1

        # Second query with same text
        await handler.process_query("test query")
        # Should use cached embedding
        assert call_count == 1


class TestRetrievalStrategies:
    """Test both retrieval strategies"""

    @pytest.mark.asyncio
    async def test_agent_strategy_basic(self, handler):
        """Test agent-level retrieval strategy"""
        handler.search_backend.mock_results = [
            {
                "url": "https://agent1.example.com",
                "name": "Agent 1",
                "json_ld": json.dumps({
                    "@type": "A2AAgent",
                    "url": "https://agent1.example.com",
                    "skills": []
                }),
                "site": "test"
            }
        ]
        handler.llm_backend.mock_rankings = {
            "https://agent1.example.com": {"score": 85, "description": "Good agent"}
        }

        result = await handler.process_query("test query", retrieval_strategy="agent")

        assert result["_meta"]["response_type"] == "answer"
        assert len(result["results"]) == 1
        assert result["results"][0]["protocol"] == "a2a"
        assert result["results"][0]["score"] == 85

    @pytest.mark.asyncio
    async def test_query_strategy_aggregation(self, handler):
        """Test query-level retrieval with aggregation"""
        # Mock query documents - multiple queries for the same agent
        handler.search_backend.mock_results = [
            {
                "url": "https://agent1.example.com/query1",
                "name": "Critique my writing",
                "agent_id": "agent1",
                "agent_name": "Writing Coach",
                "agent_url": "https://agent1.example.com",
                "query": "Critique my writing",
                "query_detail": "Provide detailed feedback on writing",
                "agent_json_ld": json.dumps({
                    "@type": "A2AAgent",
                    "url": "https://agent1.example.com",
                    "skills": []
                }),
                "@search.score": 0.95
            },
            {
                "url": "https://agent1.example.com/query2",
                "name": "Translate text",
                "agent_id": "agent1",
                "agent_name": "Writing Coach",
                "agent_url": "https://agent1.example.com",
                "query": "Translate text",
                "query_detail": "Translate to another language",
                "agent_json_ld": json.dumps({
                    "@type": "A2AAgent",
                    "url": "https://agent1.example.com",
                    "skills": []
                }),
                "@search.score": 0.85
            }
        ]

        # Mock ranking for the aggregated agent
        # Use agent name as key since that's what appears in the ranking context
        handler.llm_backend.mock_rankings = {
            "Writing Coach": {"score": 90, "description": "Excellent writing assistant"}
        }

        result = await handler.process_query("help me write", retrieval_strategy="query")

        assert result["_meta"]["response_type"] == "answer"
        assert len(result["results"]) == 1
        assert result["results"][0]["protocol"] == "a2a"
        assert result["results"][0]["score"] == 90
        # Check that matched queries are included
        assert "matched_queries" in result["results"][0]
        assert len(result["results"][0]["matched_queries"]) == 2

    @pytest.mark.asyncio
    async def test_query_strategy_multiple_agents(self, handler):
        """Test query strategy aggregates multiple agents correctly"""
        handler.search_backend.mock_results = [
            # Agent 1 queries
            {
                "agent_id": "agent1",
                "agent_name": "Writing Coach",
                "agent_url": "https://agent1.example.com",
                "query": "Critique writing",
                "query_detail": "Provide feedback",
                "agent_json_ld": json.dumps({"@type": "A2AAgent", "url": "https://agent1.example.com"}),
                "@search.score": 0.95
            },
            # Agent 2 queries
            {
                "agent_id": "agent2",
                "agent_name": "Code Reviewer",
                "agent_url": "https://agent2.example.com",
                "query": "Review code",
                "query_detail": "Review code quality",
                "agent_json_ld": json.dumps({"@type": "A2AAgent", "url": "https://agent2.example.com"}),
                "@search.score": 0.85
            }
        ]

        # Use agent names as keys since that's what appears in ranking context
        handler.llm_backend.mock_rankings = {
            "Writing Coach": {"score": 90, "description": "Writing expert"},
            "Code Reviewer": {"score": 80, "description": "Code expert"}
        }

        result = await handler.process_query("help me review", retrieval_strategy="query")

        assert result["_meta"]["response_type"] == "answer"
        assert len(result["results"]) == 2
        # Should be sorted by score
        assert result["results"][0]["score"] == 90
        assert result["results"][1]["score"] == 80

    @pytest.mark.asyncio
    async def test_default_strategy_is_agent(self, handler):
        """Test that default strategy is agent-level"""
        handler.search_backend.mock_results = [
            {
                "url": "https://test.example.com",
                "name": "Test Agent",
                "json_ld": json.dumps({"@type": "A2AAgent", "url": "https://test.example.com"}),
                "site": "test"
            }
        ]
        handler.llm_backend.mock_rankings = {
            "https://test.example.com": {"score": 85, "description": "Good"}
        }

        # Don't specify strategy - should default to "agent"
        result = await handler.process_query("test")

        assert result["_meta"]["response_type"] == "answer"
        assert len(result["results"]) == 1


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v", "--asyncio-mode=auto"])
