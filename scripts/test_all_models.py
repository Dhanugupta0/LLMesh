import asyncio
import json
import httpx

API_URL = "http://localhost:8087/v1/chat/completions"
API_KEY = "xh-RKjVH1gjynTbV1zWJpTSJ9J3VvA"

MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openai/gpt-oss-20b:free",
    "google/gemma-4-31b-it:free",
    "z-ai/glm-5.2:free",
    "cohere/north-mini-code:free",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "z-ai/glm-5.2"
]

async def test_model(client, model):
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": False
    }
    print(f"Testing {model}...")
    try:
        response = await client.post(API_URL, headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            data = response.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            print(f"✅ {model} - OK: {content[:50]}...")
        else:
            print(f"❌ {model} - Error {response.status_code}: {response.text[:100]}")
    except Exception as e:
        print(f"❌ {model} - Exception: {e}")

async def main():
    async with httpx.AsyncClient() as client:
        for model in MODELS:
            await test_model(client, model)

if __name__ == "__main__":
    asyncio.run(main())
