using UnrealBuildTool;

public class CitySim : ModuleRules
{
	public CitySim(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicIncludePaths.AddRange(new string[] { ModuleDirectory + "/Public" });
		PrivateIncludePaths.AddRange(new string[] { ModuleDirectory + "/Private" });

		PublicDependencyModuleNames.AddRange(new string[]
		{
			"Core",
			"CoreUObject",
			"Engine"
		});
	}
}
