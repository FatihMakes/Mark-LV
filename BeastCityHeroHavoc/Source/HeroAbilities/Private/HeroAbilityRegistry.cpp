#include "HeroAbilityRegistry.h"
#include "Abilities/RaccoonAbilities.h"
#include "Abilities/ChickenAbilities.h"
#include "Abilities/CatAbilities.h"

namespace HeroAbilityRegistryInternal
{
	static TMap<FName, TSubclassOf<UHeroAbilityBase>> BuildMap()
	{
		TMap<FName, TSubclassOf<UHeroAbilityBase>> Map;

		// Raccoon
		Map.Add(TEXT("ScrapGrapple"), UScrapGrappleAbility::StaticClass());
		Map.Add(TEXT("EMPBurst"), UEMPBurstAbility::StaticClass());
		Map.Add(TEXT("JunkstormUltimate"), UJunkstormUltimate::StaticClass());

		// Chicken
		Map.Add(TEXT("RocketFlap"), URocketFlapAbility::StaticClass());
		Map.Add(TEXT("SonicSquawk"), USonicSquawkAbility::StaticClass());
		Map.Add(TEXT("EggBarrageUltimate"), UEggBarrageUltimate::StaticClass());

		// Cat
		Map.Add(TEXT("PhasePounce"), UPhasePounceAbility::StaticClass());
		Map.Add(TEXT("ReflexTime"), UReflexTimeAbility::StaticClass());
		Map.Add(TEXT("NineLivesUltimate"), UNineLivesUltimate::StaticClass());

		return Map;
	}
}

TSubclassOf<UHeroAbilityBase> UHeroAbilityRegistry::FindAbilityClass(FName AbilityId)
{
	const TSubclassOf<UHeroAbilityBase>* Found = GetAllAbilities().Find(AbilityId);
	return Found ? *Found : nullptr;
}

const TMap<FName, TSubclassOf<UHeroAbilityBase>>& UHeroAbilityRegistry::GetAllAbilities()
{
	static const TMap<FName, TSubclassOf<UHeroAbilityBase>> AllAbilities = HeroAbilityRegistryInternal::BuildMap();
	return AllAbilities;
}
