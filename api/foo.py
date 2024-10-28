from langchain_openai import ChatOpenAI
from langchain.chains import QAGenerationChain
import os
import httpx

MODEL_NAME = os.getenv("MODEL_NAME", "Meta-Llama-3.1-8B-Instruct-Q8_0.gguf")
INFERENCE_SERVER_URL = os.getenv("INFERENCE_SERVER_URL", "http://localhost:8080/v1")

llm = ChatOpenAI(
    openai_api_key="EMPTY",
    openai_api_base=INFERENCE_SERVER_URL,
    model_name=MODEL_NAME,
    http_async_client=httpx.AsyncClient(verify=False),
    http_client=httpx.Client(verify=False),
    streaming=True,
    verbose=True,
    temperature=0,
)
chain = QAGenerationChain.from_llm(llm)
print(chain.run({'text': "why"}))