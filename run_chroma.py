"""Start the local ChromaDB server — workaround for the broken `chroma` CLI.

chromadb 1.5.x routes `chroma run` through chromadb_rust_bindings.cli(sys.argv),
and the Rust arg parser silently exits (code 0, no output) when argv[0] is the
pip console-script path or a `-m` module path instead of the literal program
name. Verified on chromadb 1.5.9 / Windows: `chroma.exe run ...` and
`python -m chromadb.cli.cli run ...` both die instantly, while calling the
bindings with an explicit clean argv works. launch-windows.ps1 and the engine
watchdog start Chroma through this script for that reason.

Usage: python run_chroma.py [--host 127.0.0.1] [--port 8100] [--path data/chroma]
"""

import sys

import chromadb_rust_bindings


def main() -> None:
    args = sys.argv[1:]
    if not args:
        args = ["--host", "127.0.0.1", "--port", "8100", "--path", "data/chroma"]
    chromadb_rust_bindings.cli(["chroma", "run", *args])


if __name__ == "__main__":
    main()
