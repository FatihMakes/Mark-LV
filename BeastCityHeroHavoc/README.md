# BeastCity: Hero Havoc

Superhero open-world city demolition sandbox. **Playable species are locked to Raccoon,
Chicken, and Cat only** - this rule is enforced in code (`UHeroRulesLibrary::IsValidSpecies`),
not just content, so it can never be silently bypassed.

This folder is a self-contained Unreal Engine 5 C++ project, kept separate from the rest of
this repository (which hosts an unrelated Python assistant app) to avoid any path/tooling
collisions.

## Status: foundation vertical slice (code-only, no content yet)

What exists today compiles-by-inspection (see "Known limitation" below) and gives every
downstream system a real place to plug into:

| Module | Responsibility |
|---|---|
| `HeroCore` | `FHeroProfile` data (species/appearance/powers/progression), `UHeroRulesLibrary` species-lock + loadout validation, `UHeroProfileSubsystem` (active profile + 5 save/load preset slots), `AHeroCharacterBase` (applies a profile at runtime: grants abilities, fires an appearance hookpoint for BP/materials). |
| `HeroAbilities` | `UHeroAbilityBase` (cooldown/cost/activation framework), `UHeroAbilityComponent` (grants + activates a loadout), `UHeroAbilityRegistry` (FName id -> ability class), and all 9 signature powers (3 per species: Raccoon/Chicken/Cat) as concrete scaffolded classes. |
| `Destruction` | `ADestructibleBuildingActor` (material HP, support-graph collapse, `TakeDamage` wired to standard engine damage so abilities "just work" against buildings), `UWorldStateSubsystem` (persists destroyed-building ids to a save slot and restores them on load). |
| `CitySim` | `UCityHeatSubsystem` (0-5 wanted/heat escalation; rises on destruction/combat, decays only after a sustained calm window), ticking world subsystem. |
| `BeastCityHeroHavoc` | Primary game module. `ASandboxGameMode` boots a default hero profile (loads preset slot 0, else bootstraps the default species kit), restores persisted world destruction state, and spawns `AHeroCharacterBase` as the default pawn. |

Module dependency graph: `BeastCityHeroHavoc` -> `HeroCore` -> `HeroAbilities`; `Destruction` ->
`CitySim`; `BeastCityHeroHavoc` also depends on `Destruction` and `CitySim` directly.

## Data schema

`FHeroProfile` (see `Source/HeroCore/Public/HeroTypes.h`) matches the agreed engine-agnostic
schema: `species`, `appearance` (colors/outfit/accessories/aura), `powers`
(2 actives + 1 ultimate + 2 passives), `progression` (level/xp/unlock flags). A profile is
only ever accepted after `UHeroRulesLibrary::ValidateProfile` passes.

## What's intentionally still a scaffold

- Abilities apply generic radial damage/launch effects as placeholders for real VFX/anim/combat
  resolution - they are wired end-to-end (grant -> cooldown -> activate -> affects destructible
  actors through the standard `TakeDamage` pipeline) but not art/feel-complete.
- No maps/content yet (no district blockout, no meshes, no UI widgets). Content and Blueprint
  layers (Hero Creator UI, HUD, district blockout) are the next slice.
- `ADestructibleBuildingActor` uses a single static mesh + manual support links; a real
  fracture/physics-chunk pipeline (e.g. Chaos Destruction) is not yet integrated.

## Known limitation: build could not be verified in this environment

Unreal Engine 5.8 is installed (`C:\Program Files\Epic Games\UE_5.8`), but this machine is
missing the Windows 10 SDK (`10.0.19041.0`) and a working MSVC toolchain, so
`UnrealBuildTool` fails SDK validation before it can compile any C++ module for Win64
(and no Linux/other cross-toolchain component is installed either). All code here was written
and hand-reviewed against the actual UE 5.8 engine headers (signatures for
`UGameplayStatics::ApplyRadialDamage`, `AActor::TakeDamage`, `ACharacter::LaunchCharacter`,
`UTickableWorldSubsystem`, etc. were all confirmed against the installed engine source), but it
has **not** been run through an actual compiler. To verify on a fully-provisioned machine:

```
"C:\Program Files\Epic Games\UE_5.8\Engine\Build\BatchFiles\Build.bat" BeastCityHeroHavocEditor Win64 Development -Project="<repo>\BeastCityHeroHavoc\BeastCityHeroHavoc.uproject" -WaitMutex
```

(requires the "Desktop development with C++" Visual Studio workload + Windows 10/11 SDK).

## Next steps (in priority order)

1. Verify the actual compile on a machine with the C++ toolchain installed; fix any fallout.
2. Hero Creator UI (UMG) wired to `UHeroProfileSubsystem` (species dropdown limited to the
   3-value enum, appearance controls, loadout slots, save/load preset buttons).
3. Downtown district blockout with 20-30 `ADestructibleBuildingActor` placements.
4. HUD: health/energy, cooldown widgets, heat meter bound to `UCityHeatSubsystem`.
5. Replace placeholder ability effects with real anim/VFX per the species identity (agility +
   gadgets / chaos mobility / precision + stealth).
