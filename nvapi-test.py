import json, urllib.request, urllib.error

API_KEY = "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"
BASE = "https://integrate.api.nvidia.com/v1/chat/completions"

# Меняй модель по вкусу:
#   "z-ai/glm-5.3"
#   "deepseek-ai/deepseek-v4-flash"
#   "deepseek-ai/deepseek-v4-pro"
MODEL = "z-ai/glm-5.3"

payload = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Which number is larger, 9.11 or 9.8?"},
    ],
    "temperature": 0.5,
    "top_p": 1,
    "max_tokens": 1024,
    "stream": False,
}

req = urllib.request.Request(
    BASE,
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + API_KEY,
    },
)

try:
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    print("=== ОТВЕТ ===")
    print(data["choices"][0]["message"]["content"])
    print("\n=== USAGE ===")
    print(data.get("usage"))
except urllib.error.HTTPError as e:
    print("HTTP", e.code)
    print(e.read().decode())
except Exception as e:
    print("Error:", e)
