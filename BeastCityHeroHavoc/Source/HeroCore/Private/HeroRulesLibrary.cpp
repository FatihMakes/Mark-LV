#include "HeroRulesLibrary.h"

bool UHeroRulesLibrary::IsValidSpecies(EHeroSpecies Species)
{
	switch (Species)
	{
	case EHeroSpecies::Raccoon:
	case EHeroSpecies::Chicken:
	case EHeroSpecies::Cat:
		return true;
	default:
		return false;
	}
}

bool UHeroRulesLibrary::ValidateProfile(const FHeroProfile& Profile, FString& OutError)
{
	if (!IsValidSpecies(Profile.Species))
	{
		OutError = TEXT("Invalid species. Allowed: Raccoon, Chicken, Cat.");
		return false;
	}

	if (Profile.Powers.ActiveIds.Num() != 2)
	{
		OutError = TEXT("Active power loadout must contain exactly 2 powers.");
		return false;
	}

	if (Profile.Powers.PassiveIds.Num() != 2)
	{
		OutError = TEXT("Passive loadout must contain exactly 2 passives.");
		return false;
	}

	if (Profile.Powers.UltimateId.IsNone())
	{
		OutError = TEXT("Ultimate power must be set.");
		return false;
	}

	OutError.Reset();
	return true;
}

void UHeroRulesLibrary::GetDefaultPowerKit(EHeroSpecies Species, TArray<FName>& OutActives, FName& OutUltimate, TArray<FName>& OutPassives)
{
	OutActives.Reset();
	OutPassives.Reset();
	OutUltimate = NAME_None;

	switch (Species)
	{
	case EHeroSpecies::Raccoon:
		OutActives = { TEXT("ScrapGrapple"), TEXT("EMPBurst") };
		OutUltimate = TEXT("JunkstormUltimate");
		OutPassives = { TEXT("ScavengerInstinct"), TEXT("QuickHands") };
		break;

	case EHeroSpecies::Chicken:
		OutActives = { TEXT("RocketFlap"), TEXT("SonicSquawk") };
		OutUltimate = TEXT("EggBarrageUltimate");
		OutPassives = { TEXT("LightBones"), TEXT("PanicSprint") };
		break;

	case EHeroSpecies::Cat:
		OutActives = { TEXT("PhasePounce"), TEXT("ReflexTime") };
		OutUltimate = TEXT("NineLivesUltimate");
		OutPassives = { TEXT("NightVision"), TEXT("PerfectLanding") };
		break;

	default:
		break;
	}
}

FHeroProfile UHeroRulesLibrary::MakeDefaultProfile(EHeroSpecies Species)
{
	FHeroProfile Profile;
	Profile.Species = IsValidSpecies(Species) ? Species : EHeroSpecies::Raccoon;
	GetDefaultPowerKit(Profile.Species, Profile.Powers.ActiveIds, Profile.Powers.UltimateId, Profile.Powers.PassiveIds);
	return Profile;
}
