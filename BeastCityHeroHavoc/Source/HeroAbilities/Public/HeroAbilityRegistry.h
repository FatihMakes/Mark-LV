// Static lookup from ability FName id (as stored in FHeroPowerLoadout) to its C++ class.
// This is the single place new abilities must be registered.
#pragma once

#include "CoreMinimal.h"
#include "HeroAbilityBase.h"
#include "HeroAbilityRegistry.generated.h"

UCLASS()
class HEROABILITIES_API UHeroAbilityRegistry : public UObject
{
	GENERATED_BODY()

public:
	// Returns the ability class registered for AbilityId, or nullptr if unknown.
	static TSubclassOf<UHeroAbilityBase> FindAbilityClass(FName AbilityId);

	// All nine signature powers (3 per species), for validation/editor tooling.
	static const TMap<FName, TSubclassOf<UHeroAbilityBase>>& GetAllAbilities();
};
