# LoRA drop folder

Drop LoRA files (`.safetensors` / `.gguf`) here and sd-server picks them up —
the engine config points every diffusion model's `--lora-model-dir` at this
folder.

Use a LoRA in two ways:

1. **Prompt tag** — append `<lora:filename-stem:0.8>` to any image/video
   prompt (weight is optional, default 1.0).
2. **Style preset** — attach it to a style so it applies automatically:
   `/style save my-look lora=filename-stem:0.8 model=qwen-image`

Match the LoRA to its base architecture: a Qwen-Image LoRA only works with the
qwen-image models, a Wan LoRA with wan2.2-t2v, etc. Mismatched LoRAs are
skipped with a warning in the engine log.
