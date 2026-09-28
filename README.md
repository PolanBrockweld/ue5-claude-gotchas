# UE5 + Claude Code: field notes for AI-driven Unreal development

Practical, verified notes and a small tool for building **Unreal Engine 5.8** projects with an
**AI coding agent** (Claude Code) through the **VibeUE** in-editor MCP server and the engine's
Python API.

They come from building a complete mobile game in **Blueprints only**: gameplay, UI, save
system, navigation, PvP backend and Niagara VFX, with the agent doing most of the editor
work through tools. Most of these problems produce **no error at all**. The Blueprint compiles,
the tool reports success, and the game is quietly wrong. They are not in the engine docs,
the VibeUE skills or forum answers, so I wrote them down as I hit them.

## What's here

| File | What it is |
|---|---|
| [`GOTCHAS.md`](GOTCHAS.md) | 100+ entries in 14 areas (editor crashes, MCP bridge, Python API names, graph building, pin formats, silent Blueprint logic traps, UMG touch input, navigation, materials, Niagara by script, data tables/HTTP, automated testing, agent workflow). Each one gives the **symptom, the cause and the fix**. |
| [`CLAUDE_TEMPLATE.md`](CLAUDE_TEMPLATE.md) | A portable `CLAUDE.md` working agreement to drop into a UE5 + VibeUE project, so the agent starts with the rules instead of rediscovering them. |
| [`tools/ue_bridge.py`](tools/ue_bridge.py) | A stdlib-only client for the editor's MCP server. It keeps working after an editor restart, when the AI client has lost its tools, and runs long scripts from the editor tick with full output and no 30 s reporting limit. |

## A few examples

- `execute_python_code` **saves every dirty package, including the level**, before it runs,
  so a temporary actor you leave behind gets saved by the next call.
- `WidgetService.bind_event(..., "OnClicked", ...)` **returns True and binds nothing**.
- A Blueprint **`float` variable is 32-bit** while graph math is 64-bit, so a
  `value >= limit` check against the same expression never becomes true.
- `ReceiveActorOnClicked` **never fires with touch, even on a phone**.
- A pure node wired to N pins is evaluated N times, so one `RandomIntegerInRange` feeding
  four pins gives four different numbers.
- The navmesh agent set on the `RecastNavMesh` actor **reverts on every level load**; the real
  setting lives in project config.
- Building Niagara through the engine toolset **recompiles the whole system on every input
  change**. Duplicating an approved system and changing only materials and meshes took an
  effect from about an hour to a few minutes.

## Setup assumed

- Unreal Engine 5.8, Blueprint-only project, Windows
- [VibeUE](https://www.vibeue.com) MCP server inside the editor (`http://127.0.0.1:8000/mcp`)
- Claude Code as the agent (the notes apply to any MCP client)

## Using `ue_bridge.py`

```bash
python tools/ue_bridge.py -c "import unreal; print(unreal.SystemLibrary.get_engine_version())"
python tools/ue_bridge.py my_script.py        # run a file inside the editor
python tools/ue_bridge.py -a long_job.py      # run from the editor tick; output -> log file
```

## Contributing

Found another silent trap? Open an issue or PR in the same format: symptom, cause, fix, and
the check that would have caught it.

## License

MIT. See [LICENSE](LICENSE).
