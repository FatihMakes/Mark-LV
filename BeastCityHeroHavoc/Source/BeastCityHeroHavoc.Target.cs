using UnrealBuildTool;
using System.Collections.Generic;

public class BeastCityHeroHavocTarget : TargetRules
{
	public BeastCityHeroHavocTarget(TargetInfo Target) : base(Target)
	{
		Type = TargetType.Game;
		DefaultBuildSettings = BuildSettingsVersion.Latest;
		IncludeOrderVersion = EngineIncludeOrderVersion.Latest;

		ExtraModuleNames.AddRange(new string[] { "BeastCityHeroHavoc", "HeroCore", "HeroAbilities", "CitySim", "Destruction" });
	}
}
