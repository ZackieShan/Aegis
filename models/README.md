# Local model drop folder

Put GGUF model files (and any local model folders) **here**. Aegis scans this
folder so you can serve models fully offline without downloading anything at
run time.

```
models/
  your-model.Q4_K_M.gguf
  another-model/            # a multi-file local model directory is fine too
```

## Notes

- **Weights are never committed.** `.gitignore` ignores everything in this
  folder except this README, so large `.gguf` files stay on your machine and
  out of git. Anyone cloning the repo drops in their own weights.
- On **Windows**, Aegis serves local GGUFs through **Ollama** (there is no
  bundled `llama-server` binary). Import a GGUF with a `Modelfile`:

  ```
  # Modelfile
  FROM ./your-model.Q4_K_M.gguf
  ```

  ```
  ollama create your-model -f Modelfile
  ```

  then pick it from the model picker.
- On **macOS/Linux**, the Cookbook's *Serve* tab can launch a `llama-server`
  process directly against a file in this folder.

See the top-level `README.md` and `QUICKSTART.md` for the full setup flow.
