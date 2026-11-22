# Examples

## Create a demo library

```bash
python examples/create_demo.py ./demo-content
dedup web --root ./demo-content --open
```

The script creates:

- two photos (one resized/recompressed copy)
- two near-duplicate text files
- a DOCX pair
- two repacked zip archives
- two re-encoded videos when `ffmpeg` is available

Then open the Web UI, choose the demo directory, and press **开始扫描**.

## CLI walkthrough

```bash
dedup scan ./demo-content --report ./demo-report.json
dedup report --report ./demo-report.json
dedup apply --report ./demo-report.json          # dry run
dedup apply --report ./demo-report.json --yes    # move to .dedup-trash
```

Everything is local and no network access is required.
