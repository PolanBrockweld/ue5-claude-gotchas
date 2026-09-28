# Unreal Engine 5.8 + AI agents: the gotchas nobody wrote down

Field notes from building a full mobile game **entirely in Blueprints, driven by an AI
coding agent** (Claude Code) through the **VibeUE** in-editor MCP server and the engine's
Python API. Every entry below cost at least one debugging session. Almost all of them
share one property: **nothing reports an error** -- the Blueprint compiles `UpToDate`,
the tool returns success, and the game is quietly wrong.

Format: **symptom -> cause -> fix**. Verified on UE 5.8 (Windows) unless noted.

> Conventions: `BPS` = `unreal.BlueprintService` (VibeUE), `EAL` =
> `unreal.EditorAssetLibrary`, `BEL` = `unreal.BlueprintEditorLibrary`,
> `SDS` = `SubobjectDataSubsystem`.

## Contents
1. [Editor stability & crashes](#1-editor-stability--crashes)
2. [The AI tooling bridge (VibeUE / MCP)](#2-the-ai-tooling-bridge-vibeue--mcp)
3. [Python API names that don't match C++](#3-python-api-names-that-dont-match-c)
4. [Building Blueprint graphs by tool](#4-building-blueprint-graphs-by-tool)
5. [Pin names and value formats](#5-pin-names-and-value-formats)
6. [Silent Blueprint logic traps](#6-silent-blueprint-logic-traps)
7. [Components, CDOs and placed instances](#7-components-cdos-and-placed-instances)
8. [UMG / Slate input and layout](#8-umg--slate-input-and-layout)
9. [Navigation & movement](#9-navigation--movement)
10. [Rendering, materials & lighting](#10-rendering-materials--lighting)
11. [Niagara built by script](#11-niagara-built-by-script)
12. [Data tables, save games, HTTP](#12-data-tables-save-games-http)
13. [Testing without a human](#13-testing-without-a-human)
14. [Working with an AI agent: process rules](#14-working-with-an-ai-agent-process-rules)

---

## 1. Editor stability & crashes

**1.1 Blueprints can't be edited while PIE runs.** Graph/variable edits return an empty
GUID or "Editor is currently in a play mode". End PIE and wait 2-3 s:
```python
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
les.editor_request_end_play(); les.is_in_play_in_editor()
```

**1.2 World-lifecycle calls from editor Python during PIE hard-crash the editor.**
`open_level`, level travel, `quit_editor`, runtime `SpawnActorFromClass` driven through the
bridge. Trigger level changes from the game's own Blueprint; prefer pools of pre-placed
actors over spawning at runtime.

**1.3 Heavy asset ops inside `execute_python_code` assert and crash.** FBX import (static
mesh build) flushes the task graph from inside a task:
`Assertion failed: ++Queue(QueueIndex).RecursionGuard == 1 (TaskGraph.cpp)`.
Fix: defer onto the next tick.
```python
_g = globals(); _g["_job"] = {"ran": False, "h": None}
def _do(delta):
    st = _g["_job"]
    if st["ran"]: return
    st["ran"] = True
    try:
        t = unreal.AssetImportTask(); t.filename = r"C:\m.fbx"
        t.destination_path = "/Game/Meshes"; t.automated = True; t.save = True
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([t])
    finally:
        unreal.unregister_slate_post_tick_callback(st["h"])
_g["_job"]["h"] = unreal.register_slate_post_tick_callback(_do)
```
The same pattern is the right way to run **any** long script (see `tools/ue_bridge.py -a`).

**1.4 A duplicate widget name in `WidgetService.add_component` = stack overflow crash**
(`0xC00000FD`), not an error. The root widget can't be removed ("Cannot remove root
widget"), so an "idempotent" rebuild that deletes everything and re-adds the root crashes.
Remove only non-root children; skip what `widget_exists` reports. Also empty a WBP that
*contains* other WBPs before rebuilding the contained ones.

**1.5 Starting PIE with a stale reader Blueprint can hang the editor forever.** After
rebuilding class A, any Blueprint that reads A's members must be recompiled *before*
PIE. The log ends at `Falha ao compilar blueprint: X` and the MCP server never answers
again. Rule: whoever changes a class recompiles its readers in the same script, and
every PIE test checks `compile_status` of the involved Blueprints first.

**1.6 A running process with a dead MCP might be a modal dialog, not a crash.**
`fill_data_table_from_json_string` with a missing field, or `delete_asset` on a
referenced asset, opens a **modal** and blocks the game thread. Take a screenshot before
`taskkill`; clicking OK recovers everything.

**1.7 `CrashReportClientEditor` running does not prove a crash.** An `ensure` also launches
it and prints a stack trace, then the editor keeps running. Check: does the bridge answer?
Does the log have `Fatal error`/`Assertion failed`, or only `EnsureFailed`?

**1.8 Compiling Blueprints in bulk can zero placed actors' transforms (in memory).**
After compiling ~130 Blueprints, editor-spawned actors of those classes sat at (0,0,0).
Disk was intact and `get_dirty_map_packages()` was empty. Fix: `LevelEditorSubsystem.
load_level(...)`. **Never save the level in that state.**

**1.9 Never delete `Binaries/` or `Intermediate/`** to "fix" something -- it triggers a long
missing-modules rebuild. After a crash, kill `UnrealEditor` + `CrashReportClientEditor`
and relaunch (a launcher with `-ddc=InstalledNoZenLocalFallback` avoids a ZenServer DDC hang).

**1.10 Building Niagara through the engine toolset leaks memory.** Each
`SetStackInputData` recompiles the whole system (~25 s for 20 emitters) and editor memory
only grows (~0.6 GB per emitter). On a 24 GB machine this ended in "out of memory" and
once in a blue screen. Make builds resumable, check free RAM before each step, restart
the editor between systems -- and see 11.1 for the cheaper workflow.

## 2. The AI tooling bridge (VibeUE / MCP)

**2.1 The AI client connects to the editor's MCP once.** If the editor restarts, the
client's tools vanish for the whole session, but the server (`127.0.0.1:8000/mcp`) is back.
Talk to it directly: `tools/ue_bridge.py`.

**2.2 `execute_python_code` stops *reporting* after ~30 s but the code keeps running.**
Success and failure look identical ("timeout"). Run long work from the tick and write
output to a file with an end marker (`ue_bridge.py -a`).

**2.3 `execute_python_code` saves every dirty package -- including the level -- before it
runs** (read in `PythonTools.cpp`). A temporary actor you leave in the editor world is
saved by the *next* call from anyone. Create, use and destroy temporary actors in the
**same** execution (`try/finally`), and tag them for cleanup.

**2.4 VibeUE scans submitted code and refuses some patterns** (e.g.
`get_default_object(...)`, "modifying CDOs causes crashes"). In 5.8 CDO writes work and
persist. Sending a *file path* for the editor to exec sidesteps the scan.

**2.5 Uncaught exceptions can swallow all output.** Start scripts with
`import warnings; warnings.simplefilter("ignore")` and print a line after every
create/modify -- there is no rollback, the log is your undo trail.

**2.6 Half the `add_*_node` helpers in older docs don't exist.** `add_function_call_node`,
`add_math_node`, `add_comparison_node`, `add_get/set_variable_node`, `add_member_get_node`
are gone; the declarative `build_graph` (`variable_get/set`, `member_get/set`, `math`,
`comparison`, `function_call`, `cast`, `branch`, `event`, `spawner_key`) replaced them.
Always confirm with `discover_python_class('unreal.BlueprintService', method_filter=...)`.
Never guess a method name.

**2.7 Loading many skills at once overflows the tool output** (~68k chars). Load one at a time.

**2.8 Several agents sharing one editor will trip over each other.** One agent's
`does_asset_exist` during another's PIE returned False, it tried to *create* the asset and
opened a modal "replace existing object" dialog; PIE froze for 6 minutes. Every
asset-editing script checks `is_in_play_in_editor()` and exits; never end a PIE you didn't
start; give each agent its own log file.

**2.9 The editor is shared with the human.** Starting/ending PIE, editing the save file or
recompiling while the person is playing looks, from their side, like "the button does
nothing" or "the game closed". Ask before PIE; compare the editor log timestamps of their
session and your scripts before debugging a report.

## 3. Python API names that don't match C++

| You'd expect | 5.8 Python reality |
|---|---|
| `unreal.KismetMathLibrary` | `unreal.MathLibrary` |
| `unreal.KismetMaterialLibrary` | `unreal.MaterialLibrary` |
| `unreal.SlateBlueprintLibrary` | `unreal.SlateLibrary` |
| `unreal.WidgetBlueprintLibrary` | not bound (usable only as graph nodes) |
| `unreal.AIBlueprintHelperLibrary` | not bound (`FUNC AIBlueprintHelperLibrary::GetAIController` works as a node) |
| `unreal.LevelEditorPlaySettings` | not bound |
| `Vector.size()` | `v.length()` |
| `comp.set_generate_overlap_events()` | `comp.set_editor_property("generate_overlap_events", True)` |
| `FloatingPawnMovement.b_constrain_to_plane` | `constrain_to_plane` (the `b` prefix is dropped) |
| `WidgetComponent.set_widget_class()` | `set_editor_property("widget_class", cls)` |
| `EditorAssetLibrary.save_dirty_packages_with_dialog` | `EditorLoadingAndSavingUtils.save_dirty_packages(True, True)` |
| `EditorLevelLibrary.spawn_actor_from_class` | deprecated -> `EditorActorSubsystem` |
| `BEL.remove_member_variable` | doesn't exist; only `BEL.remove_unused_variables(bp)` -- which decides by itself and removed 4 variables when asked for 1. Diff `list_member_variable_names` before/after. |
| `GetWidgetFromName` | not exposed; with `is_variable=True`, use `widget.get_editor_property("Name")` |

More:
- `HitResult` is opaque in Python (fields protected). Use `hr.export_text()` + regex.
- `FGeometry` arrives empty in Python; get screen coords from a capture instead.
- `ProgressBar.get_percent()` doesn't exist -> `get_editor_property("percent")`.
- `str(enum_value)` is `'<SlateVisibility.COLLAPSED: 1>'` -- upper case. Tests comparing to
  `"Collapsed"` fail on correct code.
- `get_editor_property` of a **struct** returns the same live wrapper each call:
  `a = x.get_editor_property("When"); b = x.get_editor_property("When"); a is b == True`.
  Use `.copy()` for a "before" value.
- `BEL.get_blueprint_parent_class(bp) != unreal.Pawn` is always True (different objects);
  compare `get_name() == "Pawn"`.
- `BEL.set_blueprint_variable_instance_editable` wants the Blueprint object, not a path.
- `EnumStructService.get_struct_info(p).properties[i].name` (not `.property_name`).
- A new `UserDefinedStruct` ships with a stray bool (`MemberVar_0`) -- remove it or it
  becomes a column in every data table row.
- `EAL.load_asset()` can return `None` during PIE. Read assets before starting PIE.
- The CDO has no components: `get_default_object(cls).get_component_by_class(...)` is None.
  Read the component template via `SDS.k2_gather_subobject_data_for_blueprint` ->
  `k2_find_subobject_data_from_handle` -> `SubobjectDataBlueprintFunctionLibrary.get_object`
  (template names end in `_GEN_VARIABLE`).
- Exporting an engine texture with `TextureExporterPNG` asserted and left the editor's
  Python broken (`SystemError: unknown opcode`) until restart.

## 4. Building Blueprint graphs by tool

**4.1 `build_graph` counts the links you asked for; the missing ones are invisible.**
(a) a node whose exec input you forgot to wire never runs; (b) an exec **output** pin
accepts only one link -- a second link silently *replaces* the first. Lint every graph:
```python
for n in BPS.get_nodes_in_graph(bp, G, 0, "", False):
    ins = [p for p in BPS.get_node_pins(bp, G, n.node_id)
           if p.is_input and p.pin_type == "exec"]
    if ins and not any(p.is_connected for p in ins):
        errors.append("never runs: %s" % n.node_title)
```
(`get_connections` returns empty while PIE is up.)

**4.2 `success` is not success.** Check `connections_failed`, `nodes_failed`,
`defaults_failed` and `warnings` -- a block with 7/11 links reported success. And check the
bool returned by `compile_blueprint`: an uncompiled Blueprint runs *no* events at all.

**4.3 `build_graph` may return `None` if any node spec is invalid, yet still create the
valid nodes.** A naive retry duplicates event nodes. Find orphans (in `get_nodes_in_graph`
but not in `get_connections`), delete them, finish with `connect_nodes`.

**4.4 Spawner keys:** `discover_nodes` returns `SPAWN K2Node_X|Title`, but `build_graph`
wants `NODE K2Node_X` (generic) or `FUNC Class::Function`. `discover_nodes` returning
nothing does not mean the function doesn't exist (`FUNC ScrollBox::ScrollToEnd` works);
ask the Python binding instead.

**4.5 Nodes `build_graph` can't make:** Create Widget (`NODE K2Node_CreateWidget`, not
found by discovery), macros like ForEachLoop/DoOnce (`add_macro_instance_node`),
`K2Node_GetArrayItem`, `K2Node_HttpRequest` (`create_node_by_key`).

**4.6 Refs collide case-insensitively.** `ReadThings` and `ReadThingS` are the same ref:
one node is created and the other's links become one warning among hundreds. Check with
`collections.Counter(r.lower() for r in refs)`.

**4.7 Calling your own function:** `class: "Self"` is rejected. Use the Blueprint's generated
class path (`/Game/.../BP_X.BP_X_C`) and leave `self` unconnected. The event must exist and
the Blueprint must be compiled first.

**4.8 No member-set on external objects via old helpers**, but `variable_set` writes your
own variables, including inherited native ones (`bShowMouseCursor`).

**4.9 Integer math/comparison doesn't exist in `math`/`comparison` nodes.** `Integer` is
rejected; use `FUNC KismetMathLibrary::Greater_IntInt`, `EqualEqual_IntInt`, etc. Doubles:
`Add_DoubleDouble`, not `_FloatFloat`.

**4.10 Some "getter" nodes are not pure.** `GetActorOfClass`, `GetAllActorsWithTag`,
`GetAllActorsOfClass`, `DoesSaveGameExist` have exec pins: wiring only the output makes
the compiler *remove* the node, which then breaks the whole Blueprint. Conversely
`Replace`, `DeprojectScreenPositionToWorld`, `ProjectWorldLocationToScreen`,
`PanelWidget::GetChildAt` are pure -- wiring exec into them fails.

**4.11 An "owner" script that grew old will delete other people's work.** A setup script
written when a Blueprint had one event started with "delete all nodes and rebuild". A year
later that graph had 1,072 nodes from other scripts. Symptom on next PIE:
`Fatal error: Failed to find function X in Y_C`. Before running an old script, read what
it deletes.

**4.12 Overrides with return values become function graphs.** `OnMouseButtonDown/Move/Up`,
`OnDrop` etc. have an entry + result node. A `K2Node_FunctionResult` can't be deleted
(`delete_node` returns False): `BEL.remove_function_graph` + `override_function` again.
Use one return node, choosing values with `K2Node_Select`.

**4.13 Variable type strings are unguessable.** Structs: raw name `"FMyStruct"` (a path
returns False). `FVector2D` not `Vector2D`. Components need the `U` prefix
(`"UExponentialHeightFogComponent"`), plain widgets don't (`"UserWidget"`, `"ProgressBar"`).
`"TSubclassOf<AActor>"` works. Variable names are
truncated at 19 characters. Test in a scratch Blueprint first.

**4.14 A component and a variable share one namespace.** Declaring variable `Sign` made
`add_component(..., "Sign")` fail in a child Blueprint, and the script aborted before
writing the CDO.

## 5. Pin names and value formats

| Node | Pin you'd guess | Real pin |
|---|---|---|
| `GetDataTableRow` output | `OutRow` | `ReturnValue` (and a 2nd exec out `RowNotFound`) |
| `SetActorHiddenInGame` | `NewHidden` | `bNewHidden` |
| `RandomPointInBoundingBox` | `Origin/BoxExtent` | `Center/HalfSize` |
| Cast output | `AsBP_Thing` | `AsBP Thing` (with spaces) |
| `K2Node_GetArrayItem` | `Index` / `ReturnValue` | `Dimension 1` / `Output` |
| `ForLoop` | `First Index` | `FirstIndex` / `LastIndex` (ForEachLoop has `Array Element`) |
| `Conv_NameToString` | `Name` | `InName` |
| `ShowOnlyActorComponents` | `Actor` | `InActor` |
| `HttpRequest` exec outs | `OnSuccess` | `On Success` / `On Error` (with space) |
| `JsonBlueprintUtilities FromString` | `JsonObject` | `OutJsonObject` |
| Button bind target class | `UButton` (as in docs) | `Button` or `/Script/UMG.Button` |

Value formats:
- **FVector** pin: `"(X=..,Y=..,Z=..)"`. **FRotator** pin: `"P, Y, R"` (bare numbers). The
  wrong format is a *warning* ("Schema dropped value ... type mismatch?") and the value
  becomes zero. Read the pin's `default_value` first; count `defaults_failed`.
- **Class pins never keep `pin_defaults`**, and a class set later with `set_node_pin_value`
  can vanish on a recompile triggered elsewhere. Store the class in a
  `TSubclassOf<...>` **variable** (lives in the CDO) and wire a getter.
- **Wildcard pins drop defaults silently** when the type resolves (`Array_Add.NewItem`,
  `Array_Set.Item`, `HttpRequest.Body`). Feed them by wire from an already-typed node
  (`Add_IntInt(A=value, B=0)`).
- **Array-typed `variable_set` rejects literals** and breaks compilation ("must have a
  connected input, try MakeArray"). Build arrays once with `Array_Add`, then `Array_Set`.
  But `set_variable_default_value` *does* accept array literals: `"(8.0,12.0)"`,
  `'("a","b")'`, `"(true,false)"` -- great for data-driven configs.
- **A `pin_default` on a pin that also receives a wire is ignored** silently.
- A string default can contain a real newline (`"\n"`).
- `K2Node_GetDataTableRow` / `K2Node_BreakStruct` start untyped: set `DataTable` /
  `StructType`, `refresh_node`, *then* wire. UserDefinedStruct break pins are mangled
  (`Field_6_C924DF42...`): read them with `get_node_pins`, and strip the **last two**
  `_`-segments (field names may contain underscores).

## 6. Silent Blueprint logic traps

**6.1 Pure nodes re-evaluate at every use.** `Add_IntInt(Get X, 1)` wired into `Set X` *and*
a later comparison compares against X+2. With `RandomIntegerInRange` feeding 4 pins you
get 4 different random numbers. With `min(Gain, CAP - Array[i])` feeding an `Array_Set`
and two counters, the counters see the already-updated array and add zero. **Rule: any
value used more than once, or whose inputs the chain itself changes, goes into a
variable first.** Also: reading an array item *after* `Array_Set` returns the new value --
swapping two items needs both old values saved first.

**6.2 `variable_set` with no value compiles and writes the pin default** (`0`, `false`,
`""`). Lint: every `variable_set` must be a link target or have a pin default.

**6.3 Blueprint `float` variables are 32-bit, graph math is 64-bit.** Storing a computed
double and comparing it back with `>=` against the same expression never becomes true.
Compare with a margin. Same with float32 data-table columns: `40 * 1.80` = 71.99999 ->
truncates to 71. Multiply in double and `Round`. (Python's `round()` is banker's rounding;
Unreal's `Round` rounds half up -- don't use one to check the other.)

**6.4 `GetDataTableRow` that fails returns a zeroed struct.** Zero is not neutral for a
multiplier. `Select` a neutral value (there's a `Select*` for every type, including
`SelectName`).

**6.5 Empty FName comes back as `"None"`.** `Conv_NameToString(None) == "None"`, and data
table JSON export writes `"None"`. Use `IsNone` / `EqualEqual_NameName`.

**6.6 `X = X * Mult` is only right while it happens once.** When a one-time buff became a
multi-step ladder, multipliers compounded. Keep the base and apply `Base * Mult`.

**6.7 Two end conditions in two actors race.** Both "victory" and "defeat" fired in the same
frame. Resolve at the moment of each hit, not by actor tick order.

**6.8 A "cancel" that completes over future frames isn't a synchronous cancel.** Code that
cancels and immediately issues a new action must close the state itself.

**6.9 Every "I'm doing X" state needs a timeout, and every position a sanity bound**
(large -- during a stage transition position and reference can briefly disagree).

**6.10 Widgets don't tick when hidden -- or when the editor window is minimized.**
UMG `Tick` is driven by Slate paint. With the editor minimized, the game world keeps
running at ~3 Hz but no widget ticks. Bring the editor to the foreground before measuring
UI (`IsIconic` on the main window).

**6.11 Save data isn't loaded when the GameMode's BeginPlay runs.** Widgets created there
read empty state; fill them on first Tick. And `RemoveFromParent` inside `Construct` does
nothing (Construct runs inside `AddToViewport`); decide *before* creating the widget.

**6.12 "Once a day" needs a stored date.** Timers and `GetTimeSeconds` die with the app.
Store `Year*1000 + DayOfYear` from `Now` (local time).

**6.13 Runaway-loop detector ends PIE ("Infinite loop detected").** It counts 1,000,000
instructions per top-level event. An inventory storing one entry per unit, counted with a
ForEach per button, blew it with one click. Build a count table once per redraw and use
`Array_Find`. No crash dump means it was the detector, not a crash.

## 7. Components, CDOs and placed instances

- **CDO writes work** (`get_default_object(cls).set_editor_property(...)`, including default
  subobjects like `cdo.character_movement.max_walk_speed`) -- but compile + save after, or
  PIE uses the old value. `set_variable_default_value` returns False for class-typed
  inherited props; `set_component_property` silently fails for FName/class props
  (`CollisionProfileName`, `ChildActorClass`): edit the component template instead.
- **Rebuilding a component tree makes placed instances stale**: meshes become `None` at the
  actor origin, and collision stays the old profile (clicks stop working) while the
  template reads correct. Destroy and respawn instances; verify on the **instance**.
- **`add_component` does reach placed instances, but template edits made *afterwards* don't**
  (widget space, pivot, relative location). Apply the same setter to each instance and
  save their packages (`EditorLoadingAndSavingUtils.save_packages([a.get_package()], False)`).
- **Cleanup by same-name leaves renamed components orphaned.** Delete everything *not* in the
  new list. `list_components`/`get_component_hierarchy` may return empty; walk subobjects.
- **A child Blueprint's component can't attach to a parent Blueprint's component** --
  `add_component` returns True, reports the parent, and attaches to root. Find by tag.
- **A child re-declaring a parent's variable** leaves the child in `BS_ERROR` and its
  instances spawn with no components.
- **Static mobility components don't move at runtime** -- `SetActorLocation` is ignored
  (but `SetActorScale3D` works, which hides the cause). Set all to `MOVABLE`.
- **Brush/volume actor scale reverts** (e.g. NavMeshBoundsVolume): set `brush_builder` x/y/z.
- **ChildActorComponent under a scaled root inherits the scale**, and the hierarchy report
  hides it. Prefer standalone placed actors.
- **Reparent Actor -> Pawn preserves the component tree** (Character would force its own root).
- **BP variables aren't instance-editable by default**: writing on a live PIE instance
  raises "cannot be edited on instances". Flip with `BEL.set_blueprint_variable_instance_editable`.
- **`set_editor_property` on an editor-world actor reruns the construction script** and
  resets non-editable variables.
- **Recompiling a Blueprint makes other Blueprints' reads of it stale** until they are
  recompiled too (see 1.5).

## 8. UMG / Slate input and layout

**8.1 `WidgetService.bind_event(..., "OnClicked", ...)` returns True and does nothing.**
Bind at runtime in `Event Construct`: `add_delegate_bind_node(wbp, g, "Button",
"OnClicked")` + `add_create_delegate_node(wbp, g, "Handler")`.

**8.2 `capture_preview` lies about layout** (Automatic slots get zero height). Judge UMG only
from a real PIE frame (VibeUE `capture_image(source="game")`). Viewport screenshots don't
include UMG at all.

**8.3 Slot layout isn't a property.** Use `CanvasPanelSlot.set_anchors/offsets/alignment/
auto_size/position/size`. Reach widgets via `unreal.find_object(wbp, "WidgetTree")` then
`find_object(tree, "Name")`.

**8.4 Buttons can't be dragged across.** `SButton` captures the pointer on press and
releases when the cursor leaves. For "hold and follow the finger" use a transparent
**Slider**: `OnMouseCaptureBegin/End`, and `GetValue()` *is* the finger position (0..1).

**8.5 Touch is not click.**
- In PIE, the mouse acts as a finger only with `bUseMouseForTouch=True`.
- Buttons inside a ScrollBox need `TouchMethod=PreciseTap`, or the button's capture kills
  the scroll drag.
- `ReceiveActorOnClicked` **never fires with touch, even on a phone** -- use
  `ReceiveActorOnInputTouchBegin` (wire both into the same node).
- A Slider's tap never reaches its delegates under touch (`OnTouchStarted` handles without
  capture; `OnTouchEnded` without capture is Unhandled). And with touch
  `OnMouseCaptureEnd` fires **twice** per gesture. Guard with a flag.
- Vertical Slider: 0 at the bottom. `OnMouseCaptureBegin` fires *before* the value commits.

**8.6 `Visible` does not swallow input; `Handled` does.** Border/Image/TextBlock return
Unhandled and the event bubbles to the viewport, which turns it into a world click. A
window over clickable ground needs `OnMouseButtonDown -> Handled` (without capture).
Conversely a full-screen UserWidget set to `Visible` is hit-testable everywhere and
blocks the whole HUD: use `SelfHitTestInvisible` on self.

**8.7 A UserWidget outside a ScrollBox may not receive the touch "up"**: with
`CaptureDuringMouseDown` the viewport acquires capture on touch start when no widget
handled it. Return `Handled` in `OnMouseButtonDown`.

**8.8 Equal ZOrder is not an order**, at canvas or viewport level. Modals need their own
high ZOrder, `SetInputMode_UIOnlyEx` with focus (called *before* `AddToViewport`), and
restore `GameAndUI` on close.

**8.9 Buttons clip text that doesn't fit, silently.** Only a PIE capture shows it.

**8.10 Screen-space `WidgetComponent`s**: root must have a desired size (a Canvas root draws
nothing with `DrawAtDesiredSize`), they don't shrink with distance (gate by camera
distance), and the anchor must fit the *frame*.

**8.11 Two-color bar = two ProgressBars in an Overlay**, top one with a transparent
background tint.

**8.12 `OnDragDetected` override lacks the `Operation` pin** when created by tool; build
drag manually (capture on the parent, `IsUnderLocation` for the drop target).

## 9. Navigation & movement

- **The nav agent lives in project settings, not on the RecastNavMesh actor.** With empty
  `SupportedAgents` the engine stamps a fallback agent (radius 35 / height 144) on every
  level load. Put `+SupportedAgents=(...)` in `DefaultEngine.ini` (get the string from
  `unreal.NavDataConfig().export_text()`); the actor is then named `RecastNavMesh-<Name>`.
- **Clearance must exceed voxelization error**: `AgentRadius - capsuleRadius >
  CellSize * MaxSimplificationError`, or paths legitimately clip walls.
- **Real erosion is ~2x the agent radius**; doorways need ~5x the agent radius.
- **A PARTIAL path is `is_valid()` and has 2+ points.** Also check `not path.is_partial()`.
- **Test all pairs of destinations**, after reopening the editor (edits to the nav actor pass
  in-session and revert on reload).
- **Use `project_point_to_navigation` with a tight extent** (~15 cm) as the clearance test.
- **Early requests after load get partial paths** (dynamic navmesh still building) --
  retry pending requests while the pawn is idle.
- **Don't reimplement PathFollowing in Blueprint**: Pawn + FloatingPawnMovement +
  AIController + `MoveToLocation` (set `bProjectDestinationToNavigation=true`, disable
  `bUseControllerRotationYaw`).
- **FloatingPawnMovement computes Velocity from displacement**: teleports become huge
  velocities (an actor flew 42 million units away). Call `StopMovementImmediately` **after**
  the teleport, and also freeze any actor you measure *from*.
- **A destination "just outside" the furniture may still be unreachable** for a swept
  capsule. Test with `capsule_trace_single_by_profile` using the actor's profile -- beware
  "Custom" profiles (everything blocks) and radius clamping.

## 10. Rendering, materials & lighting

- **Material renders flat grey when any TextureSample's SamplerType doesn't match the
  texture compression** -- the whole material falls back, and `compile_material()` still
  returns True. Color/`TC_DEFAULT`/`SAMPLERTYPE_Color`; Normal/`TC_NORMALMAP`; grayscale/
  `TC_GRAYSCALE`/`LinearGrayscale`; packed masks/`TC_MASKS`/`Masks`.
- **The editor viewport returns stale frames** when realtime is off; nudge the camera before
  capturing.
- **`static_materials[i]` edits don't persist** (copied structs): use `sm.set_material(i, m)`.
- **Black background is usually the fog, not the sky**: `FogInscatteringLuminance` black +
  `FogMaxOpacity` 1. Use `StartDistance` so near objects keep saturation.
- **Tune lighting in PIE, not via SceneCapture2D** -- the capture was much darker than the
  game. At night disable `SetAtmosphereSunLight`; overcast needs a low
  `SkyAtmosphereAmbientContributionColorScale`.
- **Exact 9:16 framing without PIE**: SceneCapture2D + render target created with
  `RTF_RGBA8` (a float RT exports OpenEXR bytes into a `.png` name).
```python
rt = unreal.RenderingLibrary.create_render_target2d(world, 540, 960,
        unreal.TextureRenderTargetFormat.RTF_RGBA8)
cap = eas.spawn_actor_from_class(unreal.SceneCapture2D, loc, rot)
c = cap.capture_component2d
c.set_editor_property("texture_target", rt)
c.set_editor_property("capture_source", unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR)
c.capture_scene()
unreal.RenderingLibrary.export_render_target(world, rt, folder, "frame.png")
```
- **`SetViewTargetWithBlend` with blend > 0 between distant stages flies the camera across
  the map.** Use 0 for cuts.
- **3D preview of an actor in UMG**: SceneCaptureComponent2D with `UseShowOnlyList` +
  `ShowOnlyActorComponents(actor)` into an RGBA8 render target used as an Image brush.

## 11. Niagara built by script

**11.1 Don't build every effect from scratch.** Each `SetStackInputData` recompiles the whole
system. Duplicate an approved system of the same archetype (`duplicate_asset`, zero
recompiles) and change identity through Material Instances and meshes -- MI parameter
changes don't recompile Niagara. In practice this took a ~20-emitter effect from ~60 min
of assembly with repeated editor restarts to a few minutes (3.5 min measured).

**11.2 Linking a `User.*` parameter to an input of a module *added by the tool* doesn't
reach the compiled script** -- the stack says "Linked", compile is UpToDate, the game uses
the default. Constants on added modules work; links on modules that came from the
emitter template work. Pick templates that already contain the module.

**11.3 Measure particles without PIE with SimCache:**
```python
cache = L.create_niagara_sim_cache(world)
L.capture_niagara_sim_cache_immediate(cache, unreal.NiagaraSimCacheCreateParameters(),
                                      comp, True, 0.2)   # advance 0.2 s
cache.read_vector_attribute("Velocity", "Emitter", 0)
cache.read_position_attribute("Position", "Emitter", 0)
```
(`L = unreal.NiagaraSimCacheFunctionLibrary`; local-space emitters return component space.)

**11.4 `SetRendererData` writes without PostEditChange** -- the renderer reads back correct
and renders as before. Rewrite the same property with `set_editor_property` in Python.

**11.5 Recreated emitters leave the old one alive in memory** with another suffix; "highest
suffix = live" can be wrong. Mark the renderer with a unique value to find it.

**11.6 An asset opened by the toolset can't be deleted** ("in use"). Empty and rebuild.

**11.7 Grey squares = `DefaultSpriteMaterial`**: a failed material step (e.g. blocked by PIE,
where `load_asset` returns None and writes None to every renderer). Verify each renderer's
material at the end.

**11.8 `fx.MaxCPUParticlesPerEmitter` may be 1000** -- above it Niagara culls silently.

**11.9 Misc:** "Scale" on WindForce is turbulence amplitude; `AddModule` appends at the end
(re-add `SolveForcesAndVelocity` last); Niagara enums are UserDefinedEnums (read
`NewEnumeratorN` via `get_enum_info`); an unwritten `DynamicParameter` reads (1,1,1,1);
Velocity-aligned sprites follow the texture's "up"; warm up textures (`NeverStream`) and
shaders before capturing video.

**11.10 Offline bake beats runtime cost.** Modeling hero shapes (e.g. a creature head) in
Blender and rendering simulated volumes into flipbooks produced a far better effect with
*fewer* particles (peak 77 vs 131-170). Keep Blender and Unreal from being heavy at the
same time on a 24 GB machine.

## 12. Data tables, save games, HTTP

- **`fill_data_table_from_json_string` with a missing field opens a modal per field per row.**
  Validate keys against the struct first; if the table points to another struct, delete
  and recreate (compare struct names without `STRUCT_REINST_` prefix/number suffix).
- **Every PIE run overwrites the save game** if the game saves on changes. Back up before
  tests; move (don't delete) to test a fresh save.
- **HTTP in pure Blueprint**: engine plugins `HttpBlueprint` + `JsonBlueprintUtilities` ship
  compiled in 5.8 -- no C++ needed. `K2Node_HttpRequest` converts struct <-> JSON by key
  name. `On Success` fires for HTTP 400 too: verify server-side.
- **`StructToJsonString` emits pretty-printed JSON in camelCase** -- strip newlines/tabs
  before embedding as a string; normalize keys server-side.
- **Nakama RPC**: the response is wrapped in `{"payload": "<json string>"}`; the request body
  is the payload as an escaped JSON string; Lua `json_encode` writes an empty table as `{}`
  (object, not array) -- force `[]` for list fields.

## 13. Testing without a human

- **Click a UMG button from Python** with PIE up: `button.on_clicked.broadcast()` runs the real
  bound event. Slider gestures: `on_mouse_capture_begin.broadcast()`, `set_value`,
  `on_mouse_capture_end.broadcast()`. **But broadcast skips hit-testing** -- a button covered
  by another widget still "works". The only proof of input is real input.
- **Real input from outside**: bring the editor forward (ALT-key trick + `SetForegroundWindow`),
  check `WindowFromPoint` belongs to the editor, then `SetCursorPos` + `mouse_event`.
  `mouse_event` from inside `execute_python_code` doesn't reach the game (the message
  queue only runs after the script returns). Viewport origin on screen:
  `get_mouse_position_on_platform() - get_mouse_position_on_viewport() * get_viewport_scale()`.
- **Touch an actor from Python**: `actor.call_method("ReceiveActorOnInputTouchBegin",
  args=(unreal.TouchIndex.TOUCH1,))`.
- **Pause to photograph timed UI**: `GameplayStatics.set_game_paused(world, True)`; the slate
  post-tick callback keeps running.
- **Game time in background PIE runs slower than the wall clock** (~50%, or ~3 Hz ticks):
  measure with `get_time_seconds(world)`.
- **Test scripts need try/except and a time limit** in tick callbacks, or they silently stop
  advancing. The bridge may execute a file twice: put run-once guards on the `unreal`
  module object, and delete old report files before launching.
- **Several broadcasts in one `execute_python_code` share one runaway-loop counter** -- fire
  one event per call when measuring graph cost.
- **Node titles are localized** ("Evento ActorOnClicked"): match by pin names, not titles.
- **A robot that plays worse (or better) than the player measures something else.** Visit
  states in the order the game visits them; guard each reading with a state check.
- **Measure the stored quantity, then its effect, and after a reload.** A check that runs right
  after the write doesn't prove persistence.

## 14. Working with an AI agent: process rules

1. **Keep a living "gotchas" file in the repo** (this document grew from one). The agent reads
   it every session; every expensive bug becomes an entry with symptom, cause, fix and a
   check that would have caught it.
2. **"It compiled" and "the tool said success" are not evidence.** Each script ends with its own
   verification that reads back the *instance*, not the template it just wrote.
3. **Lint before you build**: unset `variable_set`s, duplicate refs, defaults on wired pins,
   unconnected exec inputs, `defaults_failed > 0`.
4. **Scripts own graphs; document the order.** Running setup scripts out of order silently
   undoes work.
5. **Chain build and test with `&&`, never `;`** -- testing a Blueprint that failed to compile
   can hang the editor.
6. **Split agents by ownership** (gameplay, VFX, docs) with explicit file boundaries, and give
   every agent the shared-editor rules: no PIE without coordination, no killing the editor,
   own log files, no commits.
7. **Watch the machine, not just the code**: memory checks before heavy steps, and a screenshot
   before any `taskkill`.
