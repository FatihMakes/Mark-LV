using UnrealBuildTool;
using System.Collections.Generic;

public class BeastCityHeroHavocEditorTarget : TargetRules
{
	public BeastCityHeroHavocEditorTarget(TargetInfo Target) : base(Target)
	{
		Type = TargetType.Editor;
		DefaultBuildSettings = BuildSettingsVersion.Latest;
		IncludeOrderVersion = EngineIncludeOrderVersion.Latest;

		ExtraModuleNames.AddRange(new string[] { "BeastCityHeroHavoc", "HeroCore", "HeroAbilities", "CitySim", "Destruction" });
	}
}
