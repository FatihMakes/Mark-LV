// Attach to a hero character: grants ability instances from a loadout and routes activation/cooldowns.
#pragma once

#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "HeroAbilityBase.h"
#include "HeroAbilityComponent.generated.h"

UCLASS(ClassGroup = (Hero), meta = (BlueprintSpawnableComponent))
class HEROABILITIES_API UHeroAbilityComponent : public UActorComponent
{
	GENERATED_BODY()

public:
	UHeroAbilityComponent();

	// Clears any granted abilities and (re)grants from the given ids. Unknown ids are skipped with a warning.
	UFUNCTION(BlueprintCallable, Category = "Hero|Abilities")
	void GrantLoadout(const TArray<FName>& ActiveIds, FName UltimateId, const TArray<FName>& PassiveIds);

	UFUNCTION(BlueprintCallable, Category = "Hero|Abilities")
	bool ActivateActive(int32 SlotIndex);

	UFUNCTION(BlueprintCallable, Category = "Hero|Abilities")
	bool ActivateUltimate();

	UFUNCTION(BlueprintPure, Category = "Hero|Abilities")
	float GetActiveCooldownRemaining(int32 SlotIndex) const;

	UFUNCTION(BlueprintPure, Category = "Hero|Abilities")
	float GetUltimateCooldownRemaining() const;

protected:
	UPROPERTY(Transient)
	TArray<TObjectPtr<UHeroAbilityBase>> ActiveAbilities;

	UPROPERTY(Transient)
	TObjectPtr<UHeroAbilityBase> UltimateAbility;

	UPROPERTY(Transient)
	TArray<TObjectPtr<UHeroAbilityBase>> PassiveAbilities;

private:
	UHeroAbilityBase* GrantOne(FName AbilityId);
};
