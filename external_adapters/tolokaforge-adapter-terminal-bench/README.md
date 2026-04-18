# tolokaforge-adapter-terminal-bench

Terminal-bench adapter plugin for [Tolokaforge](https://github.com/Toloka/tolokaforge) — an LLM tool-use benchmarking harness.

This adapter enables running [terminal-bench](https://github.com/microsoft/terminal-bench) tasks within the Tolokaforge evaluation framework.

## Installation

```bash
pip install tolokaforge-adapter-terminal-bench
```

Or install tolokaforge with the adapter included:

```bash
pip install "tolokaforge[terminal_bench]"
```

## Usage

The adapter is auto-discovered via the `tolokaforge.adapters` entry point. Once installed, Tolokaforge will automatically detect and use it for terminal-bench task packs.

## License

Apache-2.0 — see [LICENSE](https://github.com/Toloka/tolokaforge/blob/main/LICENSE).
