# UE 5.8 + Claude Code + VibeUE: working agreement (portable template)

> Copy this file into your Unreal project root as `CLAUDE.md`. Add a short project header
> above it: what you're building, where content lives, the default map, and how to launch the
> editor. Then keep adding your own hard-won lessons at the bottom. See `GOTCHAS.md` for the
> full catalogue.

**Assumed setup:** a UE 5.8 Blueprint project (no game C++ module), with **VibeUE** (the
in-editor MCP server, HTTP `:8000/mcp`, declared in `.mcp.json`). Open the editor first,
then start Claude Code from the project folder so the MCP server connects.

## 0. Use the VibeUE skill library. Don't re-derive it.

VibeUE ships lazy-loaded skills (`blueprints`, `blueprint-graphs`, `materials`, `umg-widgets`,
`enhanced-input`, `data-tables`, `niagara-systems`, `level-actors`, ...).

1. Once per session: `manage_skills(action="list")`, reached through `call_tool`.
2. Before the first edit in a domain: `manage_skills(action="load", skill_name=...)`. Load
   one skill at a time; loading several can exceed the output cap.
3. Take method names and parameters from the skill's **`vibeue_apis`** block or from
   `discover_python_class`, never from memory or from example prose.
4. If a method isn't in discovery, it doesn't exist.

Basics the skills already cover:
- UE 5.7+ math is double (`Add_DoubleDouble`).
- Node IDs are 32-character GUIDs.
- Branch pins are `then` and `else`.
- Compile after structural changes, before adding nodes.
- Re-read nodes and connections, then compile, before claiming done.

## 1. Editor safety rules

- **No Blueprint edits while PIE runs.** End PIE and wait 2-3 s.
- **No world-lifecycle calls from editor Python during PIE.** That means `open_level`,
  travel and `quit_editor`: they hard-crash the editor.
- **Run long or heavy scripts from the editor tick**, via `register_slate_post_tick_callback`
  or `tools/ue_bridge.py -a`. Never run FBX import or similar inside
  `execute_python_code` directly.
- **`execute_python_code` saves all dirty packages, including the level.** Destroy temporary
  actors in the same execution.
- **Start scripts with `import warnings; warnings.simplefilter("ignore")`**, and print
  `CREATED:` / `MODIFIED:` / `DELETED:` with full asset paths.
- **Save per asset** with `EditorAssetLibrary.save_asset(path)`.
- **After changing a class, recompile every Blueprint that reads it** before PIE.
- **Never delete `Binaries/` or `Intermediate/`.**
- **The editor is shared with the human.** Ask before starting or ending PIE. Never touch
  their save file. When a bug is reported, first ask "what was *I* running at that minute?"
- **Before killing the editor**, check three things: does the bridge answer, is there a modal
  dialog on screen, and does the log say `Fatal` or only `ensure`? Take a screenshot first.

## 2. Tooling map

- **VibeUE (`unreal-mcp`)** exposes a gateway, not one tool per service:
  - `list_toolsets`, `describe_toolset`, `call_tool`
  - `execute_python_code`
  - `capture_image(source="game")`, which is the only capture that shows UMG
  - `discover_python_class`, `discover_python_function`, `discover_python_module`
- **If the MCP client lost its connection** (the editor restarted), use
  `python tools/ue_bridge.py`.
- **For UMG, trust only a PIE capture.** `WidgetService.capture_preview` flattens auto-sized
  slots, and viewport screenshots contain no UMG.

## 3. Verification loop

1. Load the matching skill, then edit.
2. Build with `build_graph`. Check `connections_failed`, `nodes_failed`, `defaults_failed` and
   `warnings`, not just `success`.
3. Lint:
   - every exec input is connected;
   - every `variable_set` has a value;
   - no duplicate refs (case-insensitive);
   - no defaults on wired pins.
4. Compile and check the returned bool.
5. Re-read nodes and connections.
6. Start PIE only if every involved Blueprint compiles. Chain build and test with `&&`.
7. Wait outside the editor. Never `time.sleep` inside `execute_python_code`: it freezes the
   game thread.
8. Read live state from the PIE world. Measure the **placed instance**, not the template.
   Measure again after a reload for anything that must persist.
9. For UI, capture the game frame with the editor in the foreground. Minimized editor
   windows don't tick widgets.
10. End PIE and wait 2-3 s before the next edit.

## 4. Project gotchas (grow this section)

> Add one entry per expensive bug: **symptom, cause, fix**, and the check that would have
> caught it. The agent reads this file at every session start; this is how it stops repeating
> mistakes.
