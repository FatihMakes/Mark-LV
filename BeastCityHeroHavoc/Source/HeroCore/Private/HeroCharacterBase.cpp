#include "HeroCharacterBase.h"
#include "HeroRulesLibrary.h"
#include "HeroAbilityComponent.h"

AHeroCharacterBase::AHeroCharacterBase()
{
	AbilityComponent = CreateDefaultSubobject<UHeroAbilityComponent>(TEXT("AbilityComponent"));
}

bool AHeroCharacterBase::ApplyProfile(const FHeroProfile& InProfile)
{
	FString ValidationError;
	if (!UHeroRulesLibrary::ValidateProfile(InProfile, ValidationError))
	{
		UE_LOG(LogTemp, Warning, TEXT("AHeroCharacterBase::ApplyProfile rejected: %s"), *ValidationError);
		return false;
	}

	RuntimeProfile = InProfile;

	if (AbilityComponent)
	{
		AbilityComponent->GrantLoadout(RuntimeProfile.Powers.ActiveIds, RuntimeProfile.Powers.UltimateId, RuntimeProfile.Powers.PassiveIds);
	}

	BP_OnAppearanceApplied(RuntimeProfile.Appearance);
	return true;
}
