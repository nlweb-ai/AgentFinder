#!/usr/bin/env python3
"""Test LLM connection to diagnose the issue"""
import asyncio
from openai import AsyncAzureOpenAI
import os

async def test_connection():
    endpoint = os.getenv("LLM_ENDPOINT", "https://guha-m91xe3zb-westus.services.ai.azure.com")
    api_key = os.getenv("LLM_API_KEY", os.getenv("AZURE_EMBEDDING_API_KEY"))
    model = os.getenv("LLM_MODEL", "gpt-4.1")
    api_version = os.getenv("LLM_API_VERSION", "2024-05-01-preview")

    print(f"Testing connection:")
    print(f"  Endpoint: {endpoint}")
    print(f"  Model: {model}")
    print(f"  API Version: {api_version}")
    # Don't print any portion of the key. (CodeQL py/clear-text-logging-sensitive-data)
    print(f"  API Key: {'set' if api_key else 'None'}")
    print()

    client = AsyncAzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version=api_version,
        max_retries=0,
        timeout=10.0
    )

    try:
        print("Testing chat completion...")
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Say hello"}],
            max_tokens=10
        )
        print(f"✓ Success! Response: {response.choices[0].message.content}")
    except Exception as e:
        print(f"✗ Error: {e}")
        print(f"Error type: {type(e).__name__}")
        if hasattr(e, 'response'):
            print(f"Response: {e.response}")

if __name__ == "__main__":
    asyncio.run(test_connection())
