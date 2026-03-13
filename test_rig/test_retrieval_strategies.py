#!/usr/bin/env python3
"""
Test rig for comparing agent-level vs query-level retrieval strategies.

Usage:
    python test_rig/test_retrieval_strategies.py
"""
import asyncio
import sys
import json
from pathlib import Path
from typing import List, Dict, Any

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "code"))
sys.path.insert(0, str(Path(__file__).parent.parent))

import who_handler
from test_rig.mock_backends import MockVectorSearchBackend, MockLLMBackend


# Test queries
TEST_QUERIES = [
    # Writing-related queries
    {
        "query": "help me improve my writing",
        "expected_agent": "Writing Coach",
        "category": "Writing"
    },
    {
        "query": "translate text to Spanish",
        "expected_agent": "Writing Coach",
        "category": "Writing"
    },
    {
        "query": "critique my essay",
        "expected_agent": "Writing Coach",
        "category": "Writing"
    },
    # Travel-related queries
    {
        "query": "plan a trip to Japan",
        "expected_agent": "Travel Concierge",
        "category": "Travel"
    },
    {
        "query": "find hotels in Paris",
        "expected_agent": "Travel Concierge",
        "category": "Travel"
    },
    {
        "query": "book flights",
        "expected_agent": "Travel Concierge",
        "category": "Travel"
    },
    # Code-related queries
    {
        "query": "review my Python code",
        "expected_agent": "Code Reviewer",
        "category": "Code"
    },
    {
        "query": "find security vulnerabilities",
        "expected_agent": "Code Reviewer",
        "category": "Code"
    },
    {
        "query": "explain this code",
        "expected_agent": "Code Reviewer",
        "category": "Code"
    },
    # Recipe-related queries
    {
        "query": "find dinner recipes",
        "expected_agent": "Recipe Finder",
        "category": "Recipes"
    },
    {
        "query": "meal planning for the week",
        "expected_agent": "Recipe Finder",
        "category": "Recipes"
    },
    # Weather-related queries
    {
        "query": "what's the weather forecast",
        "expected_agent": "Weather Service",
        "category": "Weather"
    },
    {
        "query": "get current weather",
        "expected_agent": "Weather Service",
        "category": "Weather"
    },
]


async def run_test_query(handler: who_handler.WHOHandler, query: str, strategy: str) -> Dict[str, Any]:
    """Run a single query with specified strategy"""
    result = await handler.process_query(
        query=query,
        retrieval_strategy=strategy
    )
    return result


def analyze_result(result: Dict[str, Any], expected_agent: str) -> Dict[str, Any]:
    """Analyze query result"""
    results = result.get("results", [])

    if not results:
        return {
            "found": False,
            "rank": None,
            "score": None,
            "top_agent": None,
            "top_score": None,
            "correct": False,
            "result_count": 0,
            "matched_queries": []
        }

    top_result = results[0]
    top_agent_name = top_result.get("definition", {}).get("name", "Unknown")

    # Find rank of expected agent
    expected_rank = None
    expected_score = None
    for idx, r in enumerate(results):
        agent_name = r.get("definition", {}).get("name", "")
        if agent_name == expected_agent:
            expected_rank = idx + 1
            expected_score = r.get("score", 0)
            break

    return {
        "found": expected_rank is not None,
        "rank": expected_rank,
        "score": expected_score,
        "top_agent": top_agent_name,
        "top_score": top_result.get("score", 0),
        "correct": top_agent_name == expected_agent,
        "result_count": len(results),
        "matched_queries": top_result.get("matched_queries", [])
    }


def print_comparison_table(results: List[Dict[str, Any]]):
    """Print comparison table of both strategies"""
    print("\n" + "="*120)
    print("RETRIEVAL STRATEGY COMPARISON")
    print("="*120)
    print(f"{'Query':<35} {'Expected':<20} {'Agent Strategy':<20} {'Query Strategy':<20} {'Winner':<10}")
    print("-"*120)

    agent_wins = 0
    query_wins = 0
    ties = 0

    for test_result in results:
        query = test_result["query"][:32] + "..." if len(test_result["query"]) > 32 else test_result["query"]
        expected = test_result["expected_agent"][:17] + "..." if len(test_result["expected_agent"]) > 17 else test_result["expected_agent"]

        agent_analysis = test_result["agent_strategy"]
        query_analysis = test_result["query_strategy"]

        # Format agent strategy result
        if agent_analysis["correct"]:
            agent_str = f"✓ #{agent_analysis['rank']} ({agent_analysis['score']})"
        elif agent_analysis["found"]:
            agent_str = f"✗ #{agent_analysis['rank']} ({agent_analysis['score']})"
        else:
            agent_str = "✗ Not found"

        # Format query strategy result
        if query_analysis["correct"]:
            query_str = f"✓ #{query_analysis['rank']} ({query_analysis['score']})"
        elif query_analysis["found"]:
            query_str = f"✗ #{query_analysis['rank']} ({query_analysis['score']})"
        else:
            query_str = "✗ Not found"

        # Determine winner
        agent_correct = agent_analysis["correct"]
        query_correct = query_analysis["correct"]

        if agent_correct and query_correct:
            # Both correct - compare scores
            if agent_analysis["score"] > query_analysis["score"]:
                winner = "Agent"
                agent_wins += 1
            elif query_analysis["score"] > agent_analysis["score"]:
                winner = "Query"
                query_wins += 1
            else:
                winner = "Tie"
                ties += 1
        elif agent_correct:
            winner = "Agent"
            agent_wins += 1
        elif query_correct:
            winner = "Query"
            query_wins += 1
        else:
            # Neither correct - compare if expected agent was found
            if agent_analysis["found"] and not query_analysis["found"]:
                winner = "Agent"
                agent_wins += 1
            elif query_analysis["found"] and not agent_analysis["found"]:
                winner = "Query"
                query_wins += 1
            else:
                winner = "Tie"
                ties += 1

        print(f"{query:<35} {expected:<20} {agent_str:<20} {query_str:<20} {winner:<10}")

    print("-"*120)
    print(f"\nSummary: Agent Strategy: {agent_wins} wins | Query Strategy: {query_wins} wins | Ties: {ties}")
    print("="*120)


def print_detailed_results(results: List[Dict[str, Any]]):
    """Print detailed results for each query"""
    print("\n" + "="*120)
    print("DETAILED RESULTS")
    print("="*120)

    for idx, test_result in enumerate(results, 1):
        print(f"\n{idx}. Query: \"{test_result['query']}\"")
        print(f"   Expected: {test_result['expected_agent']}")
        print(f"   Category: {test_result['category']}")

        # Agent strategy
        agent_analysis = test_result["agent_strategy"]
        print(f"\n   Agent Strategy:")
        print(f"   - Top Result: {agent_analysis['top_agent']} (score: {agent_analysis['top_score']})")
        if agent_analysis['found']:
            print(f"   - Expected agent rank: #{agent_analysis['rank']} (score: {agent_analysis['score']})")
        else:
            print(f"   - Expected agent: Not found")
        print(f"   - Total results: {agent_analysis['result_count']}")

        # Query strategy
        query_analysis = test_result["query_strategy"]
        print(f"\n   Query Strategy:")
        print(f"   - Top Result: {query_analysis['top_agent']} (score: {query_analysis['top_score']})")
        if query_analysis['found']:
            print(f"   - Expected agent rank: #{query_analysis['rank']} (score: {query_analysis['score']})")
        else:
            print(f"   - Expected agent: Not found")
        print(f"   - Total results: {query_analysis['result_count']}")

        # Show matched queries for query strategy
        if query_analysis.get('matched_queries'):
            print(f"   - Matched queries:")
            for mq in query_analysis['matched_queries'][:3]:
                print(f"     • \"{mq.get('query', 'N/A')}\" (score: {mq.get('score', 0):.2f})")


async def main():
    """Main test rig entry point"""
    print("="*120)
    print("WHO HANDLER RETRIEVAL STRATEGY TEST RIG")
    print("="*120)
    print(f"\nTesting with {len(TEST_QUERIES)} queries...")

    # Create handler with mock backends
    handler = who_handler.WHOHandler()
    handler.search_backend = MockVectorSearchBackend()
    handler.llm_backend = MockLLMBackend()

    await handler.search_backend.initialize()
    await handler.llm_backend.initialize()

    print(f"✓ Mock backends initialized")

    # Run all test queries with both strategies
    test_results = []

    for test_case in TEST_QUERIES:
        query = test_case["query"]
        expected_agent = test_case["expected_agent"]
        category = test_case["category"]

        # Test agent strategy
        agent_result = await run_test_query(handler, query, "agent")
        agent_analysis = analyze_result(agent_result, expected_agent)

        # Test query strategy
        query_result = await run_test_query(handler, query, "query")
        query_analysis = analyze_result(query_result, expected_agent)

        test_results.append({
            "query": query,
            "expected_agent": expected_agent,
            "category": category,
            "agent_strategy": agent_analysis,
            "query_strategy": query_analysis
        })

    # Print results
    print_comparison_table(test_results)
    print_detailed_results(test_results)

    # Cleanup
    await handler.cleanup()

    print("\n✓ Test rig complete!\n")


if __name__ == "__main__":
    asyncio.run(main())
