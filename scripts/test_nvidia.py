from openai import OpenAI
import os
from dotenv import load_dotenv

load_dotenv('/home/sushi/Desktop/LLMesh/.env')

client = OpenAI(
  base_url="https://integrate.api.nvidia.com/v1",
  api_key=os.getenv("NVIDIA_NIM_API_KEY")
)

try:
    completion = client.chat.completions.create(
      model="z-ai/glm-5.2",
      messages=[{"role":"user","content":"Hi"}],
      max_tokens=10,
      stream=False
    )
    print("SUCCESS:", completion.choices[0].message.content)
except Exception as e:
    print("ERROR:", str(e))
