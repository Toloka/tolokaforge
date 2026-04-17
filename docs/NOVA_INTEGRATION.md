# Amazon Nova API Integration

This document describes how to use Amazon Nova models with Tolokaforge.

## Overview

Amazon Nova is a family of foundation models developed by Amazon that supports text, image, and video inputs with text or image outputs. The Nova API is OpenAI-compatible, making it easy to integrate with Tolokaforge's existing LLM infrastructure.

## Setup

### 1. Get a Nova API Key

1. Navigate to [nova.amazon.com](https://nova.amazon.com)
2. Log into your Amazon account
3. Go to [https://nova.amazon.com/act](https://nova.amazon.com/act)
4. Click "Generate a Key"
5. Save the generated API key

### 2. Configure Environment

Add your Nova API key to the `.env` file in your project root.

```bash
# Nova API Key
NOVA_API_KEY=your-api-key-here
```

## Available Models

The following Nova models are supported (as of November 2025):

### Text Generation Models
- `Nova Pro v3` - Latest Amazon Nova model for text generation
- `Nova Pro v3.4 (Suez)` - Optimized version with enhanced capabilities
- `Nova Pro v3.4.1` - Latest iteration with improvements
- `nova-premier-v1` - Most capable model for complex reasoning tasks
- `nova-pro-v1` - Standard multimodal understanding model
- `nova-lite-v1` - Lightweight version for faster inference
- `nova-lite-v2` - Next generation lightweight model
- `nova-micro-v1` - Ultra-lightweight text-only model

- `nova-orchestrator-v1` - Model that can generate text and images

## Configuration Example

```yaml
# config/nova_example.yaml
models:
  agent:
    provider: "nova"
    name: "Nova Pro v3"
    temperature: 0.0
    max_tokens: 4096
    seed: 42
  user:
    provider: "nova"
    name: "nova-orchestrator-v1"
    temperature: 0.2

orchestrator:
  workers: 2
  repeats: 3
  timeouts:
    turn_s: 60
    episode_s: 600
  max_turns: 20

evaluation:
  tasks_glob: "tasks/**/task.yaml"
  output_dir: "output/nova_results"
  cache_images: true
```

## Usage

Run evaluations with Nova models:

```bash
# Run with Nova configuration
uv run tolokaforge run --config config/nova_example.yaml
```

## Model Capabilities

| Model | Max Tokens | Input Types | Output Types | Tool Support |
|-------|------------|-------------|--------------|--------------|
| Nova Pro v3 | 25,000 | Text, Image, Video | Text | No |
| nova-premier-v1 | 1,000,000 | Text, Image, Video | Text | No |
| nova-orchestrator-v1 | 25,000 | Text | Text, Images | No |
| nova-lite-v2 | 25,000 | Text, Image, Video | Text | Yes |
| nova-micro-v1 | 128,000 | Text | Text | No |

## Pricing

Nova models use estimated pricing based on typical cloud AI model costs:

- **Premium models** (nova-premier-v1): $2.00 input / $6.00 output per 1M tokens
- **Standard models** (Nova Pro v3): $1.00 input / $3.00 output per 1M tokens
- **Lite models** (nova-lite-v1/v2): $0.50 input / $1.50 output per 1M tokens
- **Micro models** (nova-micro-v1): $0.20 input / $0.80 output per 1M tokens

*Note: These are estimated prices. Actual costs may vary.*

## API Details

Nova API endpoint: `https://api.nova.amazon.com/v1`

The API is fully compatible with OpenAI's chat completions format:

```bash
curl -L 'https://api.nova.amazon.com/v1/chat/completions' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -d '{
    "model": "Nova Pro v3",
    "messages": [
      {
        "role": "user", 
        "content": "Hello!"
      }
    ]
  }'
```

## Troubleshooting

### Model Not Found Error

If you get a "model not found" error:

1. Check available models: `python check_nova_models.py`
2. Verify your API key has access to the model
3. Use the exact model name as returned by the `/v1/models` endpoint

### API Key Issues

- Ensure `NOVA_API_KEY` is set in your `.env` file
- Verify the key is valid and not expired
- Check that your key has the necessary permissions

### Performance Tips

- Use `nova-micro-v1` for simple text tasks requiring fast responses
- Use `Nova Pro v3` for general-purpose tasks with good quality
- Use `nova-premier-v1` for complex reasoning that requires maximum capability
- Adjust `max_tokens` based on your use case (higher limits cost more)

## Support

For Tolokaforge integration issues, see the main project documentation.
