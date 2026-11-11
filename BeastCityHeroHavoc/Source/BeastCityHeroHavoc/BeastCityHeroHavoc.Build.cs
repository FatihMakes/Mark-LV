using UnrealBuildTool;

public class BeastCityHeroHavoc : ModuleRules
{
	public BeastCityHeroHavoc(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicDependencyModuleNames.AddRange(new string[]
		{
			"Core",
			"CoreUObject",
			"Engine",
			"InputCore",
			"HeroCore",
			"HeroAbilities",
			"CitySim",
			"Destruction"
		});
	}
}
