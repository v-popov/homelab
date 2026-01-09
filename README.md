# homelab
## 🤖 AI Homelab Stack

A containerized AI environment featuring **Ollama** for local LLM inference. This stack is designed to be self-hosted on a homelab server for private, secure AI processing.

### 🚀 Quick Start

#### 1. Deploy the Stack
Navigate to the stack directory and bring up the containers in detached mode:

```bash
cd /opt/homelab/stacks/ai
docker compose up -d

#### 2. Bootstrap the Environment
```bash
chmod +x /opt/homelab/scripts/bootstrap_ai.sh
/opt/homelab/scripts/bootstrap_ai.sh

#### 3. Verification
docker exec -it ollama ollama run qwen2.5:7b-instruct "Say hello in one sentence."

